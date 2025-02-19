# -*- coding: utf-8 -*-
#
# Dash-Electrum - lightweight Dash client
# Copyright (C) 2019 Dash Developers
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation files
# (the "Software"), to deal in the Software without restriction,
# including without limitation the rights to use, copy, modify, merge,
# publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS
# BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN
# ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
# CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import time
import asyncio
import gzip
import json
import os
import random
import threading
from collections import namedtuple, defaultdict
from struct import pack

from . import constants, util
from .constants import CHUNK_SIZE
from .crypto import sha256d
from .dash_msg import DashSMLEntry, DashQFCommitMsg
from .logging import Logger
from .simple_config import SimpleConfig
from .transaction import Transaction, BCDataStream, SerializationError
from .util import bfh, bh2u, hfu
from .verifier import SPV
from .i18n import _


MN_LIST_INSTANCE = None
DEFAULT_MN_LIST = {'protx_height': 0, 'llmq_height': 0,
                   'protx_mns': {}, 'sml_hashes': {},  # SML entries and hashes
                   'quorums': {}, 'llmq_hashes': {}}   # qfcommits and hashes
RECENT_LIST_FNAME = 'recent_protx_list.gz'
PROTX_INFO_FNAME = 'protx_info.gz'


class PartialMerkleTree(namedtuple('PartialMerkleTree', 'total hashes flags')):
    '''Class representing CPartialMerkleTree of dashd'''
    @classmethod
    def read_bytes(cls, raw_bytes):
        vds = BCDataStream()
        vds.write(raw_bytes)

        total = vds.read_uint32()
        n_hashes = vds.read_compact_size()
        hashes = []
        for n in range(n_hashes):
            hashes.append(bh2u(vds.read_bytes(32)[::-1]))
        n_flags = vds.read_compact_size()
        flags = []
        for n in range(n_flags):
            flags_n = vds.read_uchar()
            for i in range(8):
                flags.append((flags_n >> i) & 1)
        if vds.can_read_more():
            raise SerializationError('extra junk at the '
                                     'end of PartialMerkleTree')
        return PartialMerkleTree(total, hashes, flags)


class MNList(Logger):
    '''Class representing data frmom MNLISTDIFF msg'''

    LOGGING_SHORTCUT = 'M'

    DIP3_DISABLED = 0
    DIP3_ENABLED = 1
    DIP3_UNKNOWN = 2

    LLMQ_DISABLED = 0
    LLMQ_ENABLED = 1
    LLMQ_UNKNOWN = 2

    LLMQ_OFFSET = 8

    RESET_WARN_MSG = _('Do you really want to reset ProTx/LLMQ data?'
                       ' All data will be downloaded again from the network.')

    def __init__(self, network, config):
        global MN_LIST_INSTANCE
        MN_LIST_INSTANCE = self
        Logger.__init__(self)

        if config is None:
            config = {}  # Do not use mutables as default values!
        self.config = (SimpleConfig(config) if isinstance(config, dict)
                       else config)
        self.network = network
        self.dash_net = network.dash_net
        self.dash_net_enabled = config.get('run_dash_net', True)
        self.load_mns = config.get('protx_load_mns', True)

        self.recent_list_lock = threading.Lock()
        self.recent_list = recent_list = self._read_recent_list()
        self.protx_info = self._read_protx_info()
        self._last_protx_info_save_time = 0
        self.mns_outpoints = self.do_back_info_mapping()

        self.protx_height = protx_height = recent_list.get('protx_height', 1)
        self.llmq_height = recent_list.get('llmq_height', 1)
        self.protx_mns = protx_mns = recent_list.get('protx_mns', {})
        self.sml_hashes = recent_list.get('sml_hashes', {})
        self.quorums = recent_list.get('quorums', {})
        self.llmq_hashes = recent_list.get('llmq_hashes', {})

        if protx_mns:
            self.protx_state = MNList.DIP3_ENABLED
        elif protx_height > 1:
            self.protx_state = MNList.DIP3_DISABLED
        else:
            self.protx_state = MNList.DIP3_UNKNOWN

        self.diff_deleted_mns = []
        self.diff_hashes = []
        self.info_hash = ''

        # Sent Requests
        self.sent_getmnlistd = asyncio.Queue(1)
        self.sent_protx_diff = asyncio.Queue(1)

        # Wait for wallet updated before request LLMQ/ProTx diffs
        self.blockchain_loaded = False
        self.wallets_updated = False

    @staticmethod
    def get_instance():
        return MN_LIST_INSTANCE

    @staticmethod
    def calc_max_height(base_height, height):
        if (base_height + 1) % CHUNK_SIZE == 0:
            height = base_height + CHUNK_SIZE
        else:
            next_chunk = (base_height + CHUNK_SIZE) // CHUNK_SIZE
            height = next_chunk * CHUNK_SIZE - 1
        return height

    @property
    def llmq_tip(self):
        return self.network.get_local_height() - self.LLMQ_OFFSET

    @property
    def protx_loading(self):
        if not self.load_mns:
            return False
        return self.network.get_local_height() > self.protx_height

    @property
    def protx_ready(self):
        if not self.load_mns or not self.network:
            return False
        server_height = self.network.get_server_height()
        if server_height <= constants.net.DIP3_ACTIVATION_HEIGHT:
            return False
        if server_height - self.protx_height > 24:
            return False  # if more than 24 blocks difference report not ready
        if len(self.protx_mns) == 0:
            return False
        else:
            return True

    @property
    def protx_list_completeness(self):
        if not self.network:
            return 0.0
        if not self.network.interface:
            return 0.0
        server_height = self.network.get_server_height()
        act_h = constants.net.DIP3_ACTIVATION_HEIGHT
        if server_height <= act_h:
            return 0.0
        return min(1.0, (self.protx_height - act_h) / (server_height - act_h))

    @property
    def protx_info_completeness(self):
        protx_mns_cnt = len(self.protx_mns)
        protx_info_cnt = len(self.protx_info)
        return min(1.0, protx_info_cnt/protx_mns_cnt if protx_mns_cnt else 0.0)

    @property
    def llmq_loading(self):
        if not self.dash_net_enabled:
            return False
        return self.llmq_tip > self.llmq_height

    @property
    def llmq_ready(self):
        if not self.dash_net_enabled or not self.network:
            return False
        server_height = self.network.get_server_height()
        if server_height <= constants.net.DIP3_ACTIVATION_HEIGHT:
            return False
        if server_height - self.llmq_height > 24 + self.LLMQ_OFFSET:
            return False  # if more than 24 blocks difference report not ready
        if len(self.quorums) == 0:
            return False
        else:
            return True

    @property
    def llmq_human_height(self):
        if self.llmq_height > 0:
            return self.llmq_height + self.LLMQ_OFFSET
        else:
            return self.llmq_height

    def with_recent_list_lock(func):
        def func_wrapper(self, *args, **kwargs):
            with self.recent_list_lock:
                return func(self, *args, **kwargs)
        return func_wrapper

    @with_recent_list_lock
    def _read_recent_list(self):
        if not self.config.path:
            return DEFAULT_MN_LIST
        path = os.path.join(self.config.path, RECENT_LIST_FNAME)
        try:
            with gzip.open(path, 'rb') as f:
                data = f.read()
                rl = json.loads(data.decode('utf-8'))
                # Read values from hex strings
                for k, v in rl['protx_mns'].items():
                    rl['protx_mns'][k] = DashSMLEntry.from_hex(v)
                for k, v in rl['sml_hashes'].items():
                    rl['sml_hashes'][k] = bfh(v)[::-1]
                for k, v in rl['quorums'].items():
                    rl['quorums'][k] = DashQFCommitMsg.from_hex(v)
                for k, v in rl['llmq_hashes'].items():
                    rl['llmq_hashes'][k] = bfh(v)[::-1]
                return rl
                self.logger.debug(f'loaded {RECENT_LIST_FNAME}')
        except Exception as e:
            self.logger.info(f'_read_recent_list: {str(e)}')
            return DEFAULT_MN_LIST

    @with_recent_list_lock
    def _save_recent_list(self):
        if not self.config.path:
            return
        path = os.path.join(self.config.path, RECENT_LIST_FNAME)
        try:
            rl = self.recent_list
            rlc = self.recent_list.copy()
            rlc['protx_mns'] = {}
            rlc['sml_hashes'] = {}
            rlc['quorums'] = {}
            rlc['llmq_hashes'] = {}
            # Save values as hex strings
            for k, v in rl['protx_mns'].items():
                rlc['protx_mns'][k] = v.serialize(as_hex=True)
            for k, v in rl['sml_hashes'].items():
                rlc['sml_hashes'][k] = bh2u(v[::-1])
            for k, v in rl['quorums'].items():
                rlc['quorums'][k] = v.serialize(as_hex=True)
            for k, v in rl['llmq_hashes'].items():
                rlc['llmq_hashes'][k] = bh2u(v[::-1])
            s = json.dumps(rlc, indent=4)
            with gzip.open(path, 'wb') as f:
                f.write(s.encode('utf-8'))
            self.logger.debug(f'saved {RECENT_LIST_FNAME}')
        except Exception as e:
            self.logger.info(f'_save_recent_list: {str(e)}')

    def _read_protx_info(self):
        if not self.config.path:
            return {}
        path = os.path.join(self.config.path, PROTX_INFO_FNAME)
        try:
            with gzip.open(path, 'rb') as f:
                data = f.read()
                return json.loads(data.decode('utf-8'))
        except Exception as e:
            self.logger.info(f'_read_protx_info: {str(e)}')
            return {}

    def _save_protx_info(self, force=False):
        if not self.config.path:
            return
        path = os.path.join(self.config.path, PROTX_INFO_FNAME)
        now = time.time()
        recently_saved = (now - self._last_protx_info_save_time < 10)
        completed = self.protx_info_completeness >= 1
        if recently_saved and not (force or completed):
            return
        try:
            s = json.dumps(self.protx_info, indent=4)
            with gzip.open(path, 'wb') as f:
                f.write(s.encode('utf-8'))
            self._last_protx_info_save_time = now
        except Exception as e:
            self.logger.info(f'_save_protx_info: {str(e)}')

    def reset(self):
        self.recent_list['protx_height'] = self.protx_height = 1
        self.recent_list['llmq_height'] = self.llmq_height = 1
        self.recent_list['protx_mns'] = self.protx_mns = {}
        self.recent_list['sml_hashes'] = self.sml_hashes = {}
        self.recent_list['quorums'] = self.quorums = {}
        self.recent_list['llmq_hashes'] = self.llmq_hashes = {}
        self.protx_info = {}
        self.mns_outpoints = {}
        self._save_recent_list()
        self._save_protx_info(force=True)
        self.protx_state = MNList.DIP3_UNKNOWN
        self.diff_deleted_mns = []
        self.diff_hashes = []
        self.notify('mn-list-diff-updated')
        if self.dash_net_enabled:
            coro = self.dash_net.getmnlistd()
        else:
            coro = self.network.request_protx_diff()
        asyncio.run_coroutine_threadsafe(coro, self.network.asyncio_loop)

    def notify(self, key):
        if key == 'mn-list-diff-updated':
            value = {
                'state': self.protx_state,
                'deleted_mns': self.diff_deleted_mns,
                'diff_hashes': self.diff_hashes,
            }
            self.diff_deleted_mns = []
            self.diff_hashes = []
        elif key == 'mn-list-info-updated':
            value = self.info_hash
            self.info_hash = ''
        else:
            value = None
        util.trigger_callback(key, value)

    async def on_network_status(self, event):
        if not self.wallets_updated:
            return
        if (not self.dash_net_enabled
                and self.network.is_connected()
                and self.protx_loading):
            await self.network.request_protx_diff()

    async def on_bchain_updated(self, key):
        if (not self.blockchain_loaded
                and self.network.blockchain_completeness >= 1.0):
            self.blockchain_loaded = True
        if not self.blockchain_loaded:
            return
        if not self.wallets_updated:
            await self.on_wallet_updated('wallet_updated', None)

    async def on_wallet_updated(self, key, val):
        if not self.blockchain_loaded:
            return
        if not self.wallets_updated:
            for w in self.network.daemon.get_wallets().values():
                if not w.is_up_to_date():
                    return
            self.wallets_updated = True
        util.trigger_callback('network_updated')

    async def on_network_updated(self, key):
        if (not self.blockchain_loaded
                and self.network.blockchain_completeness >= 1.0):
            util.trigger_callback('blockchain_updated')
        if not self.wallets_updated:
            return
        if self.dash_net_enabled:
            if self.llmq_loading:
                await self.dash_net.getmnlistd()
            elif self.protx_loading:
                await self.dash_net.getmnlistd(get_mns=True)
        elif self.protx_loading:
            await self.network.request_protx_diff()

    async def on_network_error(self, key, val):
        await asyncio.sleep(2)
        if self.dash_net_enabled:
            if self.llmq_loading:
                await self.dash_net.getmnlistd()
            elif self.protx_loading:
                await self.dash_net.getmnlistd(get_mns=True)
        elif self.protx_loading:
            await self.network.request_protx_diff()

    async def on_dash_net_updated(self, key, *args):
        status = args[0]
        if status == 'enabled':
            self.dash_net_enabled = True
        elif status == 'disabled':
            self.dash_net_enabled = False
        if not self.wallets_updated:
            return
        if self.dash_net_enabled:
            if self.llmq_loading:
                await self.dash_net.getmnlistd()
            elif self.protx_loading:
                await self.dash_net.getmnlistd(get_mns=True)
        elif self.protx_loading:
            await self.network.request_protx_diff()

    def start(self):
        # network
        util.register_callback(self.on_protx_diff, ['protx-diff'])
        util.register_callback(self.on_protx_info, ['protx-info'])
        util.register_callback(self.on_network_status, ['status'])
        util.register_callback(self.on_network_updated, ['network_updated'])
        util.register_callback(self.on_bchain_updated, ['blockchain_updated'])
        util.register_callback(self.on_wallet_updated, ['wallet_updated'])
        # dash_net
        util.register_callback(self.on_dash_net_updated, ['dash-net-updated'])
        util.register_callback(self.on_mnlistdiff, ['mnlistdiff'])
        # MNList
        util.register_callback(self.on_network_error, ['network-error'])

    def stop(self):
        # network
        util.unregister_callback(self.on_protx_diff)
        util.unregister_callback(self.on_protx_info)
        util.unregister_callback(self.on_network_updated)
        util.unregister_callback(self.on_network_status)
        util.unregister_callback(self.on_wallet_updated)
        util.unregister_callback(self.on_bchain_updated)
        # dash_net
        util.unregister_callback(self.on_dash_net_updated)
        util.unregister_callback(self.on_mnlistdiff)
        # MNList
        util.unregister_callback(self.on_network_error)

    def get_random_mn(self):
        valid = [sml_entry for sml_entry in self.protx_mns.values()
                 if sml_entry.isValid]
        if valid:
            return random.choice(valid)

    def get_mn_by_outpoint(self, outpoint):
        protx_hash = self.mns_outpoints.get(outpoint)
        if protx_hash:
            return self.protx_mns.get(protx_hash)

    def calc_responsible_quorum(self, llmqType, request_id):
        res = []
        for q in self.quorums.values():
            if q.llmqType != llmqType:
                continue
            prehash = pack('B', q.llmqType) + q.quorumHash + request_id
            sorthash = sha256d(prehash)
            res.append((sorthash, q))
        res = sorted(res, key=lambda x: x[0])
        return res[0][1] if res else None

    def calc_merkle_root(self, hashes):
        hashes_len = len(hashes)
        if hashes_len == 0:
            hashes = [b'\x00'*32]
            hashes_len = 1
        while True:
            if hashes_len == 1:
                break
            if hashes_len % 2 == 1:
                hashes.append(hashes[-1])
                hashes_len += 1
            res = []
            for i in range(hashes_len//2):
                res.append(sha256d(hashes[i*2] + hashes[i*2+1]))
            hashes = res
            hashes_len = len(hashes)
        return hfu(hashes[0][::-1])

    def check_sml_merkle_root(self, sml_hashes_dict, cbtx_extra):
        '''Check SML merkle root on cbTx.merkleRootMNList'''
        sml_hashes = [v for k, v in
                      sorted(sml_hashes_dict.items(),
                             key=lambda x: bfh(x[0])[::-1])]
        mr_calculated = self.calc_merkle_root(sml_hashes)
        mr_cbtx = hfu(cbtx_extra.merkleRootMNList[::-1])
        if mr_calculated != mr_cbtx:
            self.logger.info('check_sml_merkle_root: SML merkle root'
                             ' differs from cbtx_extra merkle root')
            return False
        return True

    def check_llmq_merkle_root(self, llmq_hashes_dict, cbtx_extra):
        '''Check LLMQ merkle root on cbTx.merkleRootQuorums'''
        llmq_hashes = sorted(llmq_hashes_dict.values())
        mr_calculated = self.calc_merkle_root(llmq_hashes)
        mr_cbtx = hfu(cbtx_extra.merkleRootQuorums[::-1])
        if mr_calculated != mr_cbtx:
            self.logger.info('check_qfcommits_merkle_root: LLMQ merkle root'
                             ' differs from CbTx merkle root')
            return False
        return True

    def check_cbtx_merkle_root(self, cbtx, merkle_tree='', hashes=None):
        '''Check cbtx on merkle tree from protx diff or on merkle hashes'''
        if merkle_tree:
            pmt = PartialMerkleTree.read_bytes(bfh(merkle_tree)).hashes
        elif hashes is not None:
            pmt = [bh2u(h[::-1]) for h in hashes]
        else:
            self.logger.info('check_cbtx_merkle_root: one of merkle_tree'
                             ' or hashes parameters must be set')
            return False

        cbtx_txid = cbtx.txid()
        if cbtx_txid != pmt[0]:
            self.logger.info('check_cbtx_merkle_root: CbTx txid differs'
                             ' from merkle tree hash 0')
            return False

        pmt.pop(0)  # remove cbtx_txid
        if len(pmt) > 0:
            merkle_root_calculated = SPV.hash_merkle_root(pmt, cbtx_txid, 0)
        else:
            merkle_root_calculated = cbtx_txid

        cbtx_height = cbtx.extra_payload.height
        cbtx_header = self.network.blockchain().read_header(cbtx_height)
        if not cbtx_header or 'merkle_root' not in cbtx_header:
            self.logger.info('check_cbtx_merkle_root: can not read blockchain'
                             ' header to check merkle root')
            return False

        if cbtx_header['merkle_root'] != merkle_root_calculated:
            self.logger.info('check_cbtx_merkle_root: CbTx calculated merkle'
                             ' root differs from blockchain merkle root')
            return False
        return True

    async def on_mnlistdiff(self, event, value):
        '''Process and check MNListDiff payload'''
        base_height, height = value['params']
        self.logger.debug(f'on_mnlistdiff base_height={base_height}'
                          f' height={height}')
        try:
            q_base_height, q_height = self.sent_getmnlistd.get_nowait()
            if q_base_height != base_height or q_height != height:
                self.logger.info('on_mnlistdiff: queue params differs')
                return
        except asyncio.QueueEmpty:
            self.logger.info('ignore unsolicited mnlistdiff repsonse')
            return

        if base_height not in [self.llmq_height, self.protx_height]:
            return

        error = value['error']
        if error:
            self.logger.info(f'on_mnlistdiff: {error}')
            self.notify('network-error')
            return
        diff = value['result']

        def process_mnlistdiff():
            self.logger.debug('process_mnlistdiff')
            cbtx = diff.cbTx
            if cbtx.tx_type:
                if cbtx.tx_type != 5:
                    self.logger.info(f'on_mnlistdiff: unsupported CbTx'
                                     f' version={cbtx.version},'
                                     f' tx_type={cbtx.tx_type}')
                    return False
                cbtx_extra = cbtx.extra_payload
                if cbtx_extra.version > 2:
                    self.logger.info(f'on_mnlistdiff: unsupported CbTx'
                                     f' cbtx_extra.version='
                                     f'{cbtx_extra.version}')
                    return False
            else:  # classical coinbase tx (disabled dip3)
                if self.load_mns:
                    self.protx_height = height
                    self.recent_list['protx_height'] = height
                    self.protx_state = MNList.DIP3_DISABLED
                self.diff_deleted_mns = []
                self.diff_hashes = []
                self.llmq_height = height
                self.recent_list['llmq_height'] = height
                return True

            if self.load_mns and base_height == self.protx_height:
                protx_new = self.protx_mns.copy()
                sml_hashes_new = self.sml_hashes.copy()
                deleted_mns = [bh2u(h[::-1]) for h in diff.deletedMNs]
                for del_hash in deleted_mns:
                    if del_hash in protx_new:
                        del protx_new[del_hash]
                    if del_hash in sml_hashes_new:
                        del sml_hashes_new[del_hash]

                for sml_entry in diff.mnList:
                    protx_hash = bh2u(sml_entry.proRegTxHash[::-1])
                    sml_hash = sha256d(sml_entry.serialize())
                    protx_new[protx_hash] = sml_entry
                    sml_hashes_new[protx_hash] = sml_hash

            if base_height == self.llmq_height and height <= self.llmq_tip:
                quorums_new = self.quorums.copy()
                llmq_hashes_new = self.llmq_hashes.copy()
                for dq in diff.deletedQuorums:
                    del_key = f'{bh2u(dq.quorumHash[::-1])}:{dq.llmqType}'
                    if del_key in quorums_new:
                        del quorums_new[del_key]
                    if del_key in llmq_hashes_new:
                        del llmq_hashes_new[del_key]

                for nq in diff.newQuorums:
                    new_key = f'{bh2u(nq.quorumHash[::-1])}:{nq.llmqType}'
                    qfcommit_hash = sha256d(nq.serialize())
                    quorums_new[new_key] = nq
                    llmq_hashes_new[new_key] = qfcommit_hash

            if self.load_mns and base_height == self.protx_height:
                if not self.check_sml_merkle_root(sml_hashes_new,
                                                  cbtx_extra):
                    return False

            if (base_height == self.llmq_height
                    and height <= self.llmq_tip
                    and cbtx_extra.version > 1):
                if not self.check_llmq_merkle_root(llmq_hashes_new,
                                                   cbtx_extra):
                    return False

            if not self.check_cbtx_merkle_root(cbtx,
                                               hashes=diff.merkleHashes):
                return False

            cbtx_height = cbtx_extra.height
            if self.load_mns and base_height == self.protx_height:
                self.protx_height = cbtx_height
                self.recent_list['protx_height'] = cbtx_height
                self.protx_mns = protx_new
                self.recent_list['protx_mns'] = protx_new
                self.sml_hashes = sml_hashes_new
                self.recent_list['sml_hashes'] = sml_hashes_new
                self.protx_state = MNList.DIP3_ENABLED

                self.diff_deleted_mns = deleted_mns
                dh = list(map(lambda x: bh2u(x.proRegTxHash[::-1]),
                          diff.mnList))
                self.diff_hashes = dh
            else:
                self.diff_deleted_mns = []
                self.diff_hashes = []

            if base_height == self.llmq_height and height <= self.llmq_tip:
                self.llmq_height = cbtx_height
                self.recent_list['llmq_height'] = cbtx_height
                self.quorums = quorums_new
                self.recent_list['quorums'] = quorums_new
                self.llmq_hashes = llmq_hashes_new
                self.recent_list['llmq_hashes'] = llmq_hashes_new

            return True

        if await self.dash_net.loop.run_in_executor(None, process_mnlistdiff):
            if self.diff_deleted_mns:
                for h in self.diff_deleted_mns:
                    self.protx_info.pop(h, None)

            if self.diff_hashes:
                for h in list(self.protx_info.keys()):
                    if h in self.diff_hashes:
                        self.protx_info.pop(h, None)

            if self.llmq_loading:
                await self.dash_net.getmnlistd()
            elif self.protx_loading:
                await self.dash_net.getmnlistd(get_mns=True)
            self._save_recent_list()
            self.notify('mn-list-diff-updated')

    async def on_protx_diff(self, key, value):
        '''Process and check protx.diff data'''
        base_height, height = value.get('params')
        self.logger.debug(f'on_protx_diff base_height={base_height}'
                          f' height={height}')
        try:
            q_base_height, q_height = self.sent_protx_diff.get_nowait()
            if q_base_height != base_height or q_height != height:
                self.logger.info('on_protx_diff: queue params differs')
                return
        except asyncio.QueueEmpty:
            self.logger.info('ignore unsolicited protx diff repsonse')
            return

        if base_height != self.protx_height:
            # on protx diff first allowed height is 1 unlike 0 in getmnlistdiff
            if self.protx_height != 0 or base_height != 1:
                return

        error = value.get('error')
        if error:
            self.logger.info(f'on_protx_diff: {error}')
            self.notify('network-error')
            return
        diff = value.get('result')

        def process_protx_diff():
            self.logger.debug('process_protx_diff')
            cbtx = Transaction(diff.get('cbTx', ''))
            cbtx.deserialize()
            if cbtx.tx_type:
                if cbtx.tx_type != 5:
                    self.logger.info(f'on_protx_diff: unsupported CbTx'
                                     f' version={cbtx.version},'
                                     f' tx_type={cbtx.tx_type}')
                    return False
                cbtx_extra = cbtx.extra_payload
                if cbtx_extra.version > 2:
                    self.logger.info(f'on_protx_diff: unsupported CbTx'
                                     f' cbtx_extra.version='
                                     f'{cbtx_extra.version}')
                    return False
            else:  # classical coinbase tx (disabled dip3)
                self.protx_height = height
                self.recent_list['protx_height'] = height
                self.protx_state = MNList.DIP3_DISABLED
                self.diff_deleted_mns = []
                self.diff_hashes = []
                return True

            protx_new = self.protx_mns.copy()
            sml_hashes_new = self.sml_hashes.copy()
            deleted_mns = diff.get('deletedMNs', [])
            for del_hash in deleted_mns:
                if del_hash in protx_new:
                    del protx_new[del_hash]
                if del_hash in sml_hashes_new:
                    del sml_hashes_new[del_hash]

            for mn in diff.get('mnList', []):
                protx_hash = mn.get('proRegTxHash', '')
                sml_entry = DashSMLEntry.from_dict(mn)
                sml_hash = sha256d(sml_entry.serialize())
                protx_new[protx_hash] = sml_entry
                sml_hashes_new[protx_hash] = sml_hash

            if not self.check_sml_merkle_root(sml_hashes_new,
                                              cbtx_extra):
                return False

            merkle_tree = diff.get('cbTxMerkleTree')
            if not self.check_cbtx_merkle_root(cbtx,
                                               merkle_tree=merkle_tree):
                return False

            cbtx_height = cbtx_extra.height
            self.protx_mns = protx_new
            self.recent_list['protx_mns'] = protx_new
            self.sml_hashes = sml_hashes_new
            self.recent_list['sml_hashes'] = sml_hashes_new
            self.protx_height = cbtx_height
            self.recent_list['protx_height'] = cbtx_height
            self.protx_state = MNList.DIP3_ENABLED
            self.diff_deleted_mns = deleted_mns
            self.diff_hashes = list(map(lambda x: x['proRegTxHash'],
                                        diff.get('mnList', [])))
            return True

        if await self.dash_net.loop.run_in_executor(None, process_protx_diff):
            if self.diff_deleted_mns:
                for h in self.diff_deleted_mns:
                    self.protx_info.pop(h, None)

            if self.diff_hashes:
                for h in list(self.protx_info.keys()):
                    if h in self.diff_hashes:
                        self.protx_info.pop(h, None)

            if self.protx_loading:
                await self.network.request_protx_diff()
            self._save_recent_list()
            self.notify('mn-list-diff-updated')

    async def on_protx_info(self, key, value):
        self.info_hash = ''

        error = value.get('error')
        if error:
            self.logger.info(f'on_protx_info: error: {error}')
            return

        protx_info = value.get('result')
        protx_hash = protx_info.get('proTxHash', '')

        if not protx_hash:
            self.logger.info('on_protx_info: empty result')
            return

        self.protx_info[protx_hash] = protx_info

        collateralHash = protx_info.get('collateralHash')
        collateralIndex = protx_info.get('collateralIndex')
        outpoint = f'{collateralHash}:{collateralIndex}'
        self.mns_outpoints[outpoint] = protx_hash

        self.info_hash = protx_hash
        self._save_protx_info()
        self.notify('mn-list-info-updated')

    def do_back_info_mapping(self):
        mns_outpoints = {}
        for protx_hash, info in self.protx_info.items():
            collateralHash = info.get('collateralHash')
            collateralIndex = info.get('collateralIndex')
            outpoint = f'{collateralHash}:{collateralIndex}'
            mns_outpoints[outpoint] = protx_hash
        return mns_outpoints

    def process_info(self):
        self.do_back_info_mapping()
        diff_hashes = set(self.protx_mns.keys())
        info_hashes = set(self.protx_info.keys())
        return diff_hashes - info_hashes
