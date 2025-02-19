#!/usr/bin/env python
#
# Electrum - lightweight Bitcoin client
# Copyright (C) 2015 Thomas Voegtlin
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

from enum import IntEnum

from PyQt5.QtCore import (pyqtSignal, Qt, QPersistentModelIndex,
                          QModelIndex, QAbstractItemModel, QVariant,
                          QItemSelectionModel)
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (QAbstractItemView, QHeaderView, QComboBox,
                             QLabel, QMenu)

from electrum_dash.i18n import _
from electrum_dash.logging import Logger
from electrum_dash.util import block_explorer_URL, profiler
from electrum_dash.plugin import run_hook
from electrum_dash.bitcoin import is_address
from electrum_dash.wallet import InternalAddressCorruption

from .util import (MyTreeView, MONOSPACE_FONT, ColorScheme, webopen,
                   GetDataThread)


class AddressUsageStateFilter(IntEnum):
    ALL = 0
    UNUSED = 1
    FUNDED = 2
    USED_AND_EMPTY = 3
    FUNDED_OR_UNUSED = 4

    def ui_text(self) -> str:
        return {
            self.ALL: _('All'),
            self.UNUSED: _('Unused'),
            self.FUNDED: _('Funded'),
            self.USED_AND_EMPTY: _('Used'),
            self.FUNDED_OR_UNUSED: _('Funded or Unused'),
        }[self]


class AddressTypeFilter(IntEnum):
    ALL = 0
    RECEIVING = 1
    CHANGE = 2

    def ui_text(self) -> str:
        return {
            self.ALL: _('All'),
            self.RECEIVING: _('Receiving'),
            self.CHANGE: _('Change'),
        }[self]


class PSStateFilter(IntEnum):
    ALL = 0
    PS = 1
    REGULAR = 2

    def ui_text(self) -> str:
        return {
            self.ALL: _('All'),
            self.PS: _('PrivateSend'),
            self.REGULAR: _('Regular'),
        }[self]


class KeystoreFilter(IntEnum):
    MAIN = 0
    PS_KS = 1
    ALL = 2

    def ui_text(self) -> str:
        return {
            self.ALL: _('All'),
            self.PS_KS: _('PS Keystore'),
            self.MAIN: _('Main'),
        }[self]


class AddrColumns(IntEnum):
    TYPE = 0
    ADDRESS = 1
    LABEL = 2
    COIN_BALANCE = 3
    FIAT_BALANCE = 4
    NUM_TXS = 5
    PS_TYPE = 6
    KEYSTORE_TYPE = 7


class AddressModel(QAbstractItemModel, Logger):

    data_ready = pyqtSignal()

    SELECT_ROWS = QItemSelectionModel.Rows | QItemSelectionModel.Select

    SORT_KEYS = {
        AddrColumns.TYPE: lambda x: (x['is_ps_ks'], x['addr_type'], x['ix']),
        AddrColumns.ADDRESS: lambda x: x['addr'],
        AddrColumns.LABEL: lambda x: x['label'],
        AddrColumns.COIN_BALANCE: lambda x: x['balance'],
        AddrColumns.FIAT_BALANCE: lambda x: x['fiat_balance'],
        AddrColumns.NUM_TXS: lambda x: x['num_txs'],
        AddrColumns.PS_TYPE: lambda x: x['is_ps'],
        AddrColumns.KEYSTORE_TYPE: lambda x: x['is_ps_ks'],
    }

    def __init__(self, parent):
        super(AddressModel, self).__init__(parent)
        Logger.__init__(self)
        self.parent = parent
        self.wallet = self.parent.wallet
        self.addr_items = list()
        # setup bg thread to get updated data
        self.data_ready.connect(self.on_get_data, Qt.QueuedConnection)
        self.get_data_thread = GetDataThread(self, self.get_addresses,
                                             self.data_ready, self)
        self.get_data_thread.start()

    def set_view(self, address_list):
        self.view = address_list
        self.view.refresh_headers()

    def headerData(self, section, orientation, role):
        if role != Qt.DisplayRole:
            return
        fx = self.parent.fx
        if fx and fx.get_fiat_address_config():
            ccy = fx.get_currency()
        else:
            ccy = _('Fiat')
        return {
            AddrColumns.TYPE: _('Type'),
            AddrColumns.ADDRESS: _('Address'),
            AddrColumns.LABEL: _('Label'),
            AddrColumns.COIN_BALANCE: _('Balance'),
            AddrColumns.FIAT_BALANCE: ccy + ' ' + _('Balance'),
            AddrColumns.NUM_TXS: _('Tx'),
            AddrColumns.PS_TYPE: _('PS Type'),
            AddrColumns.KEYSTORE_TYPE: _('Keystore'),
        }[section]

    def flags(self, idx):
        extra_flags = Qt.NoItemFlags
        if idx.column() in self.view.editable_columns:
            extra_flags |= Qt.ItemIsEditable
        return super().flags(idx) | extra_flags

    def columnCount(self, parent: QModelIndex):
        return len(AddrColumns)

    def rowCount(self, parent: QModelIndex):
        return len(self.addr_items)

    def index(self, row: int, column: int, parent: QModelIndex):
        if not parent.isValid():  # parent is root
            if len(self.addr_items) > row:
                return self.createIndex(row, column, self.addr_items[row])
        return QModelIndex()

    def parent(self, index: QModelIndex):
        return QModelIndex()

    def hasChildren(self, index: QModelIndex):
        return not index.isValid()

    def sort(self, col, order):
        if self.addr_items:
            self.process_changes(self.sorted(self.addr_items, col, order))

    def sorted(self, addr_items, col, order):
        return sorted(addr_items, key=self.SORT_KEYS[col], reverse=order)

    def data(self, index: QModelIndex, role: Qt.ItemDataRole) -> QVariant:
        assert index.isValid()
        w = self.wallet
        col = index.column()
        addr_item = index.internalPointer()
        addr_type = addr_item['addr_type']
        addr = addr_item['addr']
        is_frozen = addr_item['is_frozen']
        is_beyond_limit = addr_item['is_beyond_limit']
        is_ps = addr_item['is_ps']
        is_ps_ks = addr_item['is_ps_ks']
        label = addr_item['label']
        balance = addr_item['balance']
        fiat_balance = addr_item['fiat_balance']
        num_txs = addr_item['num_txs']
        if role not in (Qt.DisplayRole, Qt.EditRole):
            if role == Qt.TextAlignmentRole:
                if col != AddrColumns.FIAT_BALANCE:
                    return QVariant(Qt.AlignVCenter)
                else:
                    return QVariant(Qt.AlignRight | Qt.AlignVCenter)
            elif role == Qt.FontRole:
                if col not in (AddrColumns.TYPE, AddrColumns.LABEL,
                               AddrColumns.PS_TYPE, AddrColumns.KEYSTORE_TYPE):
                    return QVariant(QFont(MONOSPACE_FONT))
            elif role == Qt.BackgroundRole:
                if col == AddrColumns.TYPE:
                    if addr_type == 0:
                        return QVariant(ColorScheme.GREEN.as_color(True))
                    else:
                        return QVariant(ColorScheme.YELLOW.as_color(True))
                elif col == AddrColumns.ADDRESS:
                    if is_frozen:
                        return QVariant(ColorScheme.BLUE.as_color(True))
                    elif is_beyond_limit:
                        return QVariant(ColorScheme.RED.as_color(True))
            elif role == Qt.ToolTipRole and col == AddrColumns.TYPE:
                return QVariant(w.get_address_path_str(addr, ps_ks=is_ps_ks))
        elif col == AddrColumns.TYPE:
            return QVariant(_('receiving') if addr_type == 0 else _('change'))
        elif col == AddrColumns.ADDRESS:
            return QVariant(addr)
        elif col == AddrColumns.LABEL:
            return QVariant(label)
        elif col == AddrColumns.COIN_BALANCE:
            return QVariant(balance)
        elif col == AddrColumns.FIAT_BALANCE:
            return QVariant(fiat_balance)
        elif col == AddrColumns.NUM_TXS:
            return QVariant(num_txs)
        elif col == AddrColumns.PS_TYPE:
            return QVariant(_('PrivateSend') if is_ps else _('Regular'))
        elif col == AddrColumns.KEYSTORE_TYPE:
            return QVariant(_('PS Keystore') if is_ps_ks else _('Main'))
        else:
            return QVariant()

    @profiler
    def get_addresses(self):
        addr_items = []

        ps_addrs = self.wallet.db.get_ps_addresses()
        show_change = self.view.show_change
        show_used = self.view.show_used
        show_ps = self.view.show_ps
        show_ps_ks = self.view.show_ps_ks
        w = self.wallet

        if show_ps_ks in [KeystoreFilter.ALL, KeystoreFilter.PS_KS]:
            if show_change == AddressTypeFilter.RECEIVING:
                ps_ks_addrs = w.psman.get_receiving_addresses()
            elif show_change == AddressTypeFilter.CHANGE:
                ps_ks_addrs = w.psman.get_change_addresses()
            else:
                ps_ks_addrs = w.psman.get_addresses()
        else:  # Regular
            ps_ks_addrs = []

        if show_ps_ks in [KeystoreFilter.ALL, KeystoreFilter.MAIN]:
            if show_change == AddressTypeFilter.RECEIVING:
                main_ks_addrs = w.get_receiving_addresses()
            elif show_change == AddressTypeFilter.CHANGE:
                main_ks_addrs = w.get_change_addresses()
            else:
                main_ks_addrs = w.get_addresses()
        else:
            main_ks_addrs = []

        if show_ps == PSStateFilter.ALL:
            main_ks_addrs = [(addr, False) for addr in main_ks_addrs]
            ps_ks_addrs = [(addr, True) for addr in ps_ks_addrs]
        elif show_ps == PSStateFilter.PS:
            main_ks_addrs = [(addr, False) for addr in main_ks_addrs
                             if addr in ps_addrs]
            ps_ks_addrs = [(addr, True) for addr in ps_ks_addrs
                           if addr in ps_addrs]
        else:
            main_ks_addrs = [(addr, False) for addr in main_ks_addrs
                             if addr not in ps_addrs]
            ps_ks_addrs = [(addr, True) for addr in ps_ks_addrs
                           if addr not in ps_addrs]

        addrs_beyond_gap_limit = w.get_all_known_addresses_beyond_gap_limit()
        ps_ks_beyond_gap = w.psman.get_all_known_addresses_beyond_gap_limit()
        fx = self.parent.fx
        for i, (addr, is_ps_ks) in enumerate(main_ks_addrs + ps_ks_addrs):
            balance = sum(w.get_addr_balance(addr))
            is_used_and_empty = w.is_used(addr) and balance == 0
            if (show_used == AddressUsageStateFilter.UNUSED
                    and (balance or is_used_and_empty)):
                continue
            if show_used == AddressUsageStateFilter.FUNDED and balance == 0:
                continue
            if (show_used == AddressUsageStateFilter.USED_AND_EMPTY
                    and not is_used_and_empty):
                continue
            if (show_used == AddressUsageStateFilter.FUNDED_OR_UNUSED
                    and is_used_and_empty):
                continue

            balance_text = self.parent.format_amount(balance, whitespaces=True)
            if fx and fx.get_fiat_address_config():
                rate = fx.exchange_rate()
                fiat_balance = fx.value_str(balance, rate)
            else:
                fiat_balance = ''

            if is_ps_ks:
                is_beyond_limit = addr in ps_ks_beyond_gap
            else:
                is_beyond_limit = addr in addrs_beyond_gap_limit
            addr_items.append({
                'ix': i,
                'addr_type': 1 if w.is_change(addr) else 0,
                'addr': addr,
                'is_frozen': w.is_frozen_address(addr),
                'is_beyond_limit': is_beyond_limit,
                'label': w.get_label(addr),
                'balance': balance_text,
                'fiat_balance': fiat_balance,
                'num_txs': w.get_address_history_len(addr),
                'is_ps': True if addr in ps_addrs else False,
                'is_ps_ks': is_ps_ks,
            })
        return addr_items

    @profiler
    def process_changes(self, addr_items):
        selected = self.view.selectionModel().selectedRows()
        selected_addrs = []
        for idx in selected:
            selected_addrs.append(idx.internalPointer()['addr'])

        if self.addr_items:
            self.beginRemoveRows(QModelIndex(), 0, len(self.addr_items)-1)
            self.addr_items.clear()
            self.endRemoveRows()

        if addr_items:
            self.beginInsertRows(QModelIndex(), 0, len(addr_items)-1)
            self.addr_items = addr_items[:]
            self.endInsertRows()

        selected_rows = []
        if selected_addrs:
            for i, addr_item in enumerate(addr_items):
                addr = addr_item['addr']
                if addr in selected_addrs:
                    selected_rows.append(i)
                    selected_addrs.remove(addr)
                    if not selected_addrs:
                        break
        if selected_rows:
            for i in selected_rows:
                idx = self.index(i, 0, QModelIndex())
                self.view.selectionModel().select(idx, self.SELECT_ROWS)

    def on_get_data(self):
        self.refresh(self.get_data_thread.res)

    @profiler
    def refresh(self, addr_items):
        self.view.refresh_headers()
        if addr_items == self.addr_items:
            return
        col = self.view.header().sortIndicatorSection()
        order = self.view.header().sortIndicatorOrder()
        self.process_changes(self.sorted(addr_items, col, order))
        self.view.filter()


class AddressList(MyTreeView):

    filter_columns = [AddrColumns.TYPE, AddrColumns.ADDRESS,
                      AddrColumns.LABEL, AddrColumns.COIN_BALANCE,
                      AddrColumns.PS_TYPE, AddrColumns.KEYSTORE_TYPE]

    def __init__(self, parent, model):
        stretch_column = AddrColumns.LABEL
        super(AddressList, self).__init__(parent, self.create_menu,
                                          stretch_column=stretch_column,
                                          editable_columns=[stretch_column])
        self.am = model
        self.setModel(model)
        self.wallet = self.parent.wallet
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)

        header = self.header()
        header.setDefaultAlignment(Qt.AlignCenter)
        header.setStretchLastSection(False)
        header.setSortIndicator(AddrColumns.TYPE, Qt.AscendingOrder)
        self.setSortingEnabled(True)
        for col in AddrColumns:
            if col == stretch_column:
                header.setSectionResizeMode(col, QHeaderView.Stretch)
            elif col in [AddrColumns.TYPE, AddrColumns.PS_TYPE,
                         AddrColumns.KEYSTORE_TYPE]:
                header.setSectionResizeMode(col, QHeaderView.Fixed)
            else:
                header.setSectionResizeMode(col, QHeaderView.ResizeToContents)

        self.show_change = AddressTypeFilter.ALL  # type: AddressTypeFilter
        self.show_used = AddressUsageStateFilter.ALL  # type: AddressUsageStateFilter
        self.show_ps = PSStateFilter.ALL
        self.show_ps_ks = KeystoreFilter.ALL
        self.change_button = QComboBox(self)
        self.change_button.currentIndexChanged.connect(self.toggle_change)
        for addr_type in AddressTypeFilter.__members__.values():  # type: AddressTypeFilter
            self.change_button.addItem(addr_type.ui_text())
        self.used_button = QComboBox(self)
        self.used_button.currentIndexChanged.connect(self.toggle_used)
        for addr_usage_state in AddressUsageStateFilter.__members__.values():  # type: AddressUsageStateFilter
            self.used_button.addItem(addr_usage_state.ui_text())
        self.ps_button = QComboBox(self)
        self.ps_button.currentIndexChanged.connect(self.toggle_ps)
        for ps_state in PSStateFilter.__members__.values():
            self.ps_button.addItem(ps_state.ui_text())
        self.ps_ks_button = QComboBox(self)
        self.ps_ks_button.currentIndexChanged.connect(self.toggle_ps_ks)
        for keystore in KeystoreFilter.__members__.values():
            self.ps_ks_button.addItem(keystore.ui_text())

    def refresh_headers(self):
        fx = self.parent.fx
        if fx and fx.get_fiat_address_config():
            self.showColumn(AddrColumns.FIAT_BALANCE)
        else:
            self.hideColumn(AddrColumns.FIAT_BALANCE)

    def get_toolbar_buttons(self):
        return (QLabel('    %s ' % _("Filter Type:")),
                self.change_button,
                QLabel('    %s ' % _("Usage:")),
                self.used_button,
                QLabel('    %s ' % _("PS Type:")),
                self.ps_button,
                QLabel('    %s ' % _("Keystore:")),
                self.ps_ks_button)

    def on_hide_toolbar(self):
        self.show_change = AddressTypeFilter.ALL  # type: AddressTypeFilter
        self.show_used = AddressUsageStateFilter.ALL  # type: AddressUsageStateFilter
        self.show_ps = PSStateFilter.ALL
        self.show_ps_ks = KeystoreFilter.ALL
        self.update()

    def save_toolbar_state(self, state, config):
        config.set_key('show_toolbar_addresses', state)

    def toggle_change(self, state: int):
        if state == self.show_change:
            return
        self.show_change = AddressTypeFilter(state)
        self.update()

    def toggle_used(self, state: int):
        if state == self.show_used:
            return
        self.show_used = AddressUsageStateFilter(state)
        self.update()

    def toggle_ps(self, state):
        if state == self.show_ps:
            return
        self.show_ps = PSStateFilter(state)
        self.update()

    def toggle_ps_ks(self, state):
        if state == self.show_ps_ks:
            return
        self.show_ps_ks = KeystoreFilter(state)
        self.update()

    def update(self):
        if self.maybe_defer_update():
            return
        self.am.get_data_thread.need_update.set()

    def add_copy_menu(self, menu: QMenu, idx) -> QMenu:
        cc = menu.addMenu(_("Copy"))
        fx = self.parent.fx
        for column in AddrColumns:
            if column == AddrColumns.FIAT_BALANCE:
                if not fx or not fx.get_fiat_address_config():
                    continue
            column_title = self.am.headerData(column, None, Qt.DisplayRole)
            col_idx = idx.sibling(idx.row(), column)
            clipboard_data = self.am.data(col_idx, Qt.DisplayRole)
            clipboard_data = str(clipboard_data.value()).strip()
            cc.addAction(column_title,
                         lambda text=clipboard_data, title=column_title:
                         self.place_text_on_clipboard(text, title=title))
        return cc

    def create_menu(self, position):
        from electrum_dash.wallet import Multisig_Wallet
        is_multisig = isinstance(self.wallet, Multisig_Wallet)
        can_delete = self.wallet.can_delete_address()
        selected = self.selectionModel().selectedRows()
        if not selected:
            return
        multi_select = len(selected) > 1
        addr_items = []
        for idx in selected:
            if not idx.isValid():
                return
            addr_items.append(idx.internalPointer())
        addrs = [addr_item['addr'] for addr_item in addr_items]
        menu = QMenu()
        if not multi_select:
            idx = self.indexAt(position)
            if not idx.isValid():
                return
            item = addr_items[0]
            if not item:
                return
            addr = item['addr']
            is_ps = item['is_ps']
            is_ps_ks = item['is_ps_ks']

            hd = self.am.headerData
            addr_title = hd(AddrColumns.LABEL, None, Qt.DisplayRole)
            label_idx = idx.sibling(idx.row(), AddrColumns.LABEL)

            self.add_copy_menu(menu, idx)
            menu.addAction(_('Details'),
                           lambda: self.parent.show_address(addr))

            persistent = QPersistentModelIndex(label_idx)
            menu.addAction(_("Edit {}").format(addr_title),
                           lambda p=persistent: self.edit(QModelIndex(p)))

            #if not is_ps and not is_ps_ks:
            #    menu.addAction(_("Request payment"),
            #                   lambda: self.parent.receive_at(addr))
            if self.wallet.can_export() or self.wallet.psman.is_ps_ks(addr):
                menu.addAction(_("Private key"),
                               lambda: self.parent.show_private_key(addr))
            if not is_multisig and not self.wallet.is_watching_only():
                menu.addAction(_("Sign/verify message"),
                               lambda: self.parent.sign_verify_message(addr))
                menu.addAction(_("Encrypt/decrypt message"),
                               lambda: self.parent.encrypt_message(addr))
            if can_delete:
                menu.addAction(_("Remove from wallet"),
                               lambda: self.parent.remove_address(addr))
            addr_URL = block_explorer_URL(self.config, 'addr', addr)
            if addr_URL:
                menu.addAction(_("View on block explorer"),
                               lambda: webopen(addr_URL))

            if not is_ps:
                def set_frozen_state(addrs, state):
                    self.parent.set_frozen_state_of_addresses(addrs, state)
                if not self.wallet.is_frozen_address(addr):
                    menu.addAction(_("Freeze"),
                                   lambda: set_frozen_state([addr], True))
                else:
                    menu.addAction(_("Unfreeze"),
                                   lambda: set_frozen_state([addr], False))

        coins = self.wallet.get_spendable_coins(addrs)
        if coins:
            menu.addAction(_("Spend from"),
                           lambda: self.parent.utxo_list.set_spend_list(coins))

        run_hook('receive_menu', menu, addrs, self.wallet)
        menu.exec_(self.viewport().mapToGlobal(position))

    def place_text_on_clipboard(self, text: str, *, title: str = None) -> None:
        if is_address(text):
            try:
                if self.wallet.psman.is_ps_ks:
                    self.wallet.psman.check_address(text)
                else:
                    self.wallet.check_address_for_corruption(text)
            except InternalAddressCorruption as e:
                self.parent.show_error(str(e))
                raise
        super().place_text_on_clipboard(text, title=title)

    def hide_rows(self):
        for row in range(len(self.am.addr_items)):
            if self.current_filter:
                self.hide_row(row)
            else:
                self.setRowHidden(row, QModelIndex(), False)

    def hide_row(self, row):
        model = self.am
        for column in self.filter_columns:
            idx = model.index(row, column, QModelIndex())
            if idx.isValid():
                txt = model.data(idx, Qt.DisplayRole).value().lower()
                if self.current_filter in txt:
                    self.setRowHidden(row, QModelIndex(), False)
                    return
        self.setRowHidden(row, QModelIndex(), True)

    def get_edit_key_from_coordinate(self, row, col):
        if col == AddrColumns.LABEL:
            idx = self.am.index(row, col, QModelIndex())
            if idx.isValid():
                key_idx = idx.sibling(idx.row(), AddrColumns.ADDRESS)
                if key_idx.isValid():
                    return self.am.data(key_idx, Qt.DisplayRole).value()

    def on_edited(self, idx, edit_key, *, text):
        self.wallet.set_label(edit_key, text)
        addr_item = idx.internalPointer()
        addr_item['label'] = text
        self.am.dataChanged.emit(idx, idx, [Qt.DisplayRole])
        self.parent.history_model.refresh('address label edited')
        self.parent.utxo_list.update()
        self.parent.update_completions()
