#
# Copyright (C) 2023 github.com/ping
#
# This file is part of the OverDrive Libby Plugin by ping
# OverDrive Libby Plugin for calibre / libby-calibre-plugin
#
# See https://github.com/ping/libby-calibre-plugin for more
# information
#
from collections import namedtuple
from datetime import datetime
from typing import Dict, Optional, List

from calibre.utils.config import tweaks
from calibre.utils.date import format_date, dt_as_local
from calibre.utils.icu import lower as icu_lower
from qt.core import Qt, QAbstractTableModel, QModelIndex, QFont, QColor

from . import DEMO_MODE
from .compat import _c, hex_to_rgb
from .config import PREFS, PreferenceKeys
from .libby import LibbyClient
from .libby.client import LibbyMediaTypes
from .magazine_download_utils import extract_isbn, extract_asin
from .utils import PluginColors, PluginIcons

load_translations()


def get_media_title(
    loan: Dict, for_sorting: bool = False, include_subtitle: bool = False
) -> str:
    """
    Formats the title for a loan

    :param loan:
    :param for_sorting: If True, uses the sort attributes instead
    :param include_subtitle: If True, include subtitle
    :return:
    """
    title: str = (
        loan["sortTitle"] if for_sorting and loan.get("sortTitle") else loan["title"]
    )
    if (
        include_subtitle
        and loan.get("subtitle")
        and not title.endswith(loan["subtitle"])
    ):
        # sortTitle contains subtitle?
        title = f'{title}: {loan["subtitle"]}'
    if loan["type"]["id"] == LibbyMediaTypes.Magazine and loan.get("edition", ""):
        if not for_sorting:
            title = f'{title} - {loan.get("edition", "")}'
        else:
            title = f'{title}|{loan["id"]}'

    return title


def truncate_for_display(text, text_length=30):
    if DEMO_MODE:
        return "*" * min(len(text), text_length)
    if len(text) <= text_length:
        return text
    return text[:text_length] + "…"


LOAN_TYPE_TRANSLATION = {"ebook": _("ebook"), "magazine": _("magazine")}


class LibbyModel(QAbstractTableModel):
    column_headers = []
    DisplaySortRole = Qt.UserRole + 1000

    def __init__(self, parent, synced_state=None, db=None):
        super().__init__(parent)
        self.db = db
        self._cards = []
        self._libraries = []
        self._rows = []
        self.filtered_rows = []

    def headerData(self, section, orientation, role):
        if role != Qt.DisplayRole:
            return None
        if orientation == Qt.Vertical:
            return section + 1
        if section >= len(self.column_headers):
            return None
        return self.column_headers[section]

    def columnCount(self, parent=None):
        return len(self.column_headers)

    def rowCount(self, parent=None):
        return len(self.filtered_rows)

    def removeRows(self, row, count, _):
        self.beginRemoveRows(QModelIndex(), row, row + count - 1)
        self.filtered_rows = (
            self.filtered_rows[:row] + self.filtered_rows[row + count :]
        )
        self.endRemoveRows()
        return True

    def sync(self, synced_state: Optional[Dict] = None):
        if not synced_state:
            synced_state = {}
        self._cards = synced_state.get("cards", [])
        self._libraries = synced_state.get("__libraries", [])

    def get_card(self, card_id) -> Optional[Dict]:
        return next(
            iter([c for c in self._cards if c["cardId"] == card_id]),
            None,
        )

    def get_website_id(self, card) -> int:
        return int(card.get("library", {}).get("websiteId", "0"))

    def get_library(self, website_id: int) -> Optional[Dict]:
        return next(
            iter([l for l in self._libraries if l["websiteId"] == website_id]),
            None,
        )


LoanMatchCondition = namedtuple(
    "LoanMatchCondition", ["title1", "title2", "isbn", "asin"]
)


class LibbyLoansModel(LibbyModel):
    """
    Underlying data model for the Loans table view
    """

    column_headers = [
        _c("Title"),
        _c("Author"),
        _("Expire Date"),
        _("Library"),
        _c("Type"),
        _c("Format"),
    ]
    filter_hide_books_already_in_library = False

    def __init__(self, parent, synced_state=None, db=None, icons=None):
        super().__init__(parent, synced_state, db)
        self.icons = icons
        self.all_book_ids_titles = self.db.fields["title"].table.book_col_map
        self.all_book_ids_formats = self.db.fields["formats"].table.book_col_map
        self.all_book_ids_identifiers = self.db.fields["identifiers"].table.book_col_map
        self.filter_hide_books_already_in_library = PREFS[
            PreferenceKeys.HIDE_BOOKS_ALREADY_IN_LIB
        ]
        self.sync(synced_state)

    def sync(self, synced_state: Optional[Dict] = None):
        super().sync(synced_state)
        if not synced_state:
            synced_state = {}
        self._rows = sorted(
            synced_state.get("loans", []),
            key=lambda ln: ln["checkoutDate"],
            reverse=True,
        )
        self.filter_rows()

    def filter_rows(self):
        self.beginResetModel()
        self.filtered_rows = []
        for loan in self._rows:
            if not (
                (
                    LibbyClient.is_downloadable_ebook_loan(loan)
                    and not PREFS[PreferenceKeys.HIDE_EBOOKS]
                )
                or (
                    LibbyClient.is_downloadable_magazine_loan(loan)
                    and not PREFS[PreferenceKeys.HIDE_MAGAZINES]
                )
            ):
                continue

            try:
                loan_format = LibbyClient.get_loan_format(
                    loan, PREFS[PreferenceKeys.PREFER_OPEN_FORMATS]
                )
            except ValueError:
                continue

            if not self.filter_hide_books_already_in_library:
                # hide lib books filter is not enabled
                self.filtered_rows.append(loan)
                continue

            # hide lib books filter is enabled
            book_in_library = False
            loan_title1 = icu_lower(get_media_title(loan).strip())
            loan_title2 = icu_lower(
                get_media_title(loan, include_subtitle=True).strip()
            )
            loan_isbn = extract_isbn(loan.get("formats", []), [loan_format])
            loan_asin = extract_asin(loan.get("formats", []))
            for book_id, title in iter(self.all_book_ids_titles.items()):
                book_identifiers = self.all_book_ids_identifiers.get(book_id) or {}
                book_in_library = (
                    icu_lower(title) in (loan_title1, loan_title2)
                    or (loan_isbn and loan_isbn == book_identifiers.get("isbn", ""))
                    or (loan_asin and loan_asin == book_identifiers.get("amazon", ""))
                    or (loan_asin and loan_asin == book_identifiers.get("asin", ""))
                )
                if book_in_library:
                    if PREFS[
                        PreferenceKeys.EXCLUDE_EMPTY_BOOKS
                    ] and not self.all_book_ids_formats.get(book_id):
                        book_in_library = False
                    break  # check only first matching book

            if not book_in_library:
                self.filtered_rows.append(loan)

        self.endResetModel()

    def set_filter_hide_books_already_in_library(self, value: bool):
        if value != self.filter_hide_books_already_in_library:
            self.filter_hide_books_already_in_library = value
            self.filter_rows()

    def data(self, index, role):
        row, col = index.row(), index.column()
        if row >= self.rowCount():
            return None
        loan: Dict = self.filtered_rows[row]
        if role == Qt.UserRole:
            return loan
        if col >= self.columnCount():
            return None
        if role == Qt.ToolTipRole and col == 0:
            return get_media_title(loan, include_subtitle=True)
        if role == Qt.ToolTipRole and col == 2 and loan.get("isLuckyDayCheckout"):
            return _("A skip-the-line loan")
        if role == Qt.DecorationRole and col == 2 and loan.get("isLuckyDayCheckout"):
            return self.icons[PluginIcons.Clover]
        if role == Qt.TextAlignmentRole and col >= 2:
            return Qt.AlignCenter
        if role == Qt.ForegroundRole and col == 2 and LibbyClient.is_renewable(loan):
            return QColor(*hex_to_rgb(PluginColors.Red))
        if role not in (Qt.DisplayRole, LibbyModel.DisplaySortRole):
            return None
        if col == 0:
            if role == LibbyModel.DisplaySortRole:
                return get_media_title(loan, for_sorting=True)
            return get_media_title(loan)
        if col == 1:
            creator_name = loan.get("firstCreatorName", "")
            if role == LibbyModel.DisplaySortRole:
                return loan.get("firstCreatorSortName", "") or creator_name
            return creator_name
        if col == 2:
            dt_value = dt_as_local(LibbyClient.parse_datetime(loan["expireDate"]))
            if role == LibbyModel.DisplaySortRole:
                return dt_value.isoformat()
            if DEMO_MODE:
                return format_date(
                    dt_value.replace(month=12, day=31),
                    tweaks["gui_timestamp_display_format"],
                )
            return format_date(dt_value, tweaks["gui_timestamp_display_format"])
        if col == 3:
            card = self.get_card(loan["cardId"])
            if DEMO_MODE:
                return "*" * len(card["advantageKey"])
            return card["advantageKey"]
        if col == 4:
            type_id = loan.get("type", {}).get("id", "")
            return LOAN_TYPE_TRANSLATION.get(type_id, "") or type_id
        if col == 5:
            return str(
                LibbyClient.get_loan_format(
                    loan, PREFS[PreferenceKeys.PREFER_OPEN_FORMATS]
                )
            )
        return None


class LibbyHoldsModel(LibbyModel):
    """
    Underlying data model for the Holds table view
    """

    column_headers = [
        _c("Title"),
        _c("Author"),
        _("Hold/Expire Date"),
        _("Library"),
        _c("Format"),
        _("Available"),
    ]
    filter_hide_unavailable_holds = True

    def __init__(self, parent, synced_state=None, db=None):
        super().__init__(parent, synced_state, db)
        self.filter_hide_unavailable_holds = PREFS[
            PreferenceKeys.HIDE_HOLDS_UNAVAILABLE
        ]
        self.sync(synced_state)

    def sync(self, synced_state: Optional[Dict] = None):
        super().sync(synced_state)
        if not synced_state:
            synced_state = {}
        self._rows = sorted(
            synced_state.get("holds", []),
            key=lambda h: (
                h["isAvailable"],
                -h.get("estimatedWaitDays", 9999),
                h["placedDate"],
            ),
            reverse=True,
        )
        self.filter_rows()

    def filter_rows(self):
        self.beginResetModel()
        self.filtered_rows = []
        for hold in [
            h
            for h in self._rows
            if (
                not PREFS[PreferenceKeys.HIDE_EBOOKS]
                and LibbyClient.is_downloadable_ebook_loan(h)
            )
            or (
                not PREFS[PreferenceKeys.HIDE_MAGAZINES]
                and LibbyClient.is_downloadable_magazine_loan(h)
            )
        ]:
            if hold.get("isAvailable", False) or not self.filter_hide_unavailable_holds:
                self.filtered_rows.append(hold)
        self.endResetModel()

    def set_filter_hide_unavailable_holds(self, value: bool):
        if value != self.filter_hide_unavailable_holds:
            self.filter_hide_unavailable_holds = value
            self.filter_rows()

    def setData(self, index, hold, role=Qt.EditRole):
        if role == Qt.EditRole:
            self.filtered_rows[index.row()] = hold
            self.dataChanged.emit(index, index)
            return True

    def data(self, index, role):
        row, col = index.row(), index.column()
        if row >= self.rowCount():
            return None
        hold: Dict = self.filtered_rows[row]
        is_suspended = bool(
            hold.get("suspensionFlag") and hold.get("suspensionEnd")
        ) and not hold.get("isAvailable")

        if role == Qt.UserRole:
            return hold
        if col >= self.columnCount():
            return None
        if role == Qt.ToolTipRole and col == 0:
            return get_media_title(hold, include_subtitle=True)
        if role == Qt.ToolTipRole and col == 2 and hold.get("expireDate"):
            dt_value = dt_as_local(LibbyClient.parse_datetime(hold["expireDate"]))
            return _("Expires {dt}").format(
                dt=format_date(dt_value, tweaks["gui_timestamp_display_format"])
            )
        if role == Qt.ForegroundRole and col == 2 and hold.get("isAvailable", False):
            return QColor(*hex_to_rgb(PluginColors.Red))
        if role == Qt.TextAlignmentRole and col >= 2:
            return Qt.AlignCenter
        if role == Qt.FontRole and col == 5 and hold.get("isAvailable", False):
            font = QFont()
            font.setBold(True)
            return font
        if role == Qt.ToolTipRole and col == 5 and is_suspended:
            suspended_till = dt_as_local(
                LibbyClient.parse_datetime(hold["suspensionEnd"])
            )
            if (
                hold.get("redeliveriesRequestedCount", 0) > 0
                or hold.get("redeliveriesAutomatedCount", 0) > 0
            ):
                return _("Deliver after {dt}").format(
                    dt=format_date(
                        suspended_till, tweaks["gui_timestamp_display_format"]
                    )
                )
            else:
                return _("Suspended till {dt}").format(
                    dt=format_date(
                        suspended_till, tweaks["gui_timestamp_display_format"]
                    )
                )
        if role not in (Qt.DisplayRole, LibbyModel.DisplaySortRole):
            return None
        if col == 0:
            if role == LibbyModel.DisplaySortRole:
                return get_media_title(hold, for_sorting=True)
            return get_media_title(hold)
        if col == 1:
            creator_name = hold.get("firstCreatorName", "")
            if role == LibbyModel.DisplaySortRole:
                return hold.get("firstCreatorSortName", "") or creator_name
            return creator_name
        if col == 2:
            dt_value = dt_as_local(
                LibbyClient.parse_datetime(hold.get("expireDate") or hold["placedDate"])
            )
            if role == LibbyModel.DisplaySortRole:
                return dt_value.isoformat()
            if DEMO_MODE:
                return format_date(
                    dt_value.replace(month=1, day=1),
                    tweaks["gui_timestamp_display_format"],
                )
            return format_date(dt_value, tweaks["gui_timestamp_display_format"])
        if col == 3:
            card = self.get_card(hold["cardId"])
            if DEMO_MODE:
                return "*" * len(card["advantageKey"])
            return card["advantageKey"]
        if col == 4:
            return str(
                LibbyClient.get_loan_format(
                    hold, PREFS[PreferenceKeys.PREFER_OPEN_FORMATS]
                )
            )
        if col == 5:
            if role == LibbyModel.DisplaySortRole:
                return -1 if is_suspended else int(hold.get("isAvailable", False))
            if is_suspended:
                if (
                    hold.get("redeliveriesRequestedCount", 0) > 0
                    or hold.get("redeliveriesAutomatedCount", 0) > 0
                ):
                    return _("Delayed")
                return _("Suspended")
            return _c("Yes") if hold.get("isAvailable", False) else _c("No")

        return None


class LibbyCardsModel(LibbyModel):
    """
    Underlying data model for the Library Cards combobox
    """

    column_headers = ["Card"]

    def __init__(self, parent, synced_state=None, db=None):
        super().__init__(parent, synced_state, db)
        self.sync(synced_state)

    def sync(self, synced_state: Optional[Dict] = None):
        super().sync(synced_state)
        self._rows = self._cards
        self.filter_rows()

    def filter_rows(self):
        self.beginResetModel()
        self.filtered_rows = sorted(self._rows, key=lambda c: c["createDate"])
        self.endResetModel()

    def data(self, index, role):
        row, col = index.row(), index.column()
        if row >= self.rowCount():
            return None
        card: Dict = self.filtered_rows[row]
        if role == Qt.UserRole:
            return card
        if role != Qt.DisplayRole:
            return None
        if col == 0:
            return truncate_for_display(f'{card["advantageKey"]}: {card["cardName"]}')
        return None


class LibbyMagazinesModel(LibbyModel):
    """
    Underlying data model for the Magazines table view
    """

    column_headers = [_c("Title"), _("Release Date"), _("Library Card"), _("Borrowed")]
    filter_hide_magazines_already_in_library = False

    def __init__(self, parent, synced_state=None, db=None):
        super().__init__(parent, synced_state, db)
        self.all_book_ids_titles = self.db.fields["title"].table.book_col_map
        self.all_book_ids_formats = self.db.fields["formats"].table.book_col_map
        self._loans: List[Dict] = []
        self.filter_hide_magazines_already_in_library = PREFS[
            PreferenceKeys.HIDE_BOOKS_ALREADY_IN_LIB
        ]
        self.sync(synced_state)

    def set_filter_hide_magazines_already_in_library(self, value: bool):
        if value != self.filter_hide_magazines_already_in_library:
            self.filter_hide_magazines_already_in_library = value
            self.filter_rows()

    def sync(self, synced_state: Optional[Dict] = None):
        super().sync(synced_state)
        if not synced_state:
            synced_state = {}
        self._loans = synced_state.get("loans", [])
        self._rows = synced_state.get("__subscriptions", [])
        self.filter_rows()

    def sync_subscriptions(self, subscriptions: List[Dict]):
        self._rows = subscriptions
        self.filter_rows()

    def filter_rows(self):
        self.beginResetModel()
        self.filtered_rows = []
        for r in sorted(
            self._rows, key=lambda t: t["estimatedReleaseDate"], reverse=True
        ):
            r["__is_borrowed"] = bool([l for l in self._loans if l["id"] == r["id"]])
            if not self.filter_hide_magazines_already_in_library:
                self.filtered_rows.append(r)
                continue

            # hide lib books filter is enabled
            book_in_library = False
            q1 = icu_lower(get_media_title(r).strip())
            q2 = icu_lower(get_media_title(r, include_subtitle=True).strip())
            for book_id, title in iter(self.all_book_ids_titles.items()):
                if icu_lower(title) not in (q1, q2):
                    continue
                if (
                    not PREFS[PreferenceKeys.EXCLUDE_EMPTY_BOOKS]
                ) or self.all_book_ids_formats.get(book_id):
                    book_in_library = True
                break  # check only first matching book title
            if not book_in_library:
                self.filtered_rows.append(r)

        self.endResetModel()

    def data(self, index, role):
        row, col = index.row(), index.column()
        if row >= self.rowCount():
            return None
        subscription: Dict = self.filtered_rows[row]
        if role == Qt.UserRole:
            return subscription
        if col >= self.columnCount():
            return None
        if role == Qt.TextAlignmentRole and col >= 1:
            return Qt.AlignCenter
        if role == Qt.ToolTipRole and col == 0:
            return get_media_title(subscription, include_subtitle=True)
        if role == Qt.ToolTipRole and col == 2:
            card = self.get_card(subscription["cardId"])
            if not card:
                return "Invalid card setup"
            return f'{card["advantageKey"]}: {card["cardName"]}'
        if role not in (Qt.DisplayRole, LibbyModel.DisplaySortRole):
            return None
        if col == 0:
            return get_media_title(subscription)
        if col == 1:
            dt_value = datetime.strptime(
                subscription["estimatedReleaseDate"], "%Y-%m-%dT%H:%M:%SZ"
            )
            if role == LibbyModel.DisplaySortRole:
                return dt_value.isoformat()
            return format_date(dt_value, tweaks["gui_timestamp_display_format"])
        if col == 2:
            card = self.get_card(subscription["cardId"])
            if not card:
                return "Invalid card setup"
            return truncate_for_display(f'{card["advantageKey"]}: {card["cardName"]}')
        if col == 3:
            is_borrowed = subscription.get("__is_borrowed")
            if role == LibbyModel.DisplaySortRole:
                return int(is_borrowed)
            return _c("Yes") if is_borrowed else _c("No")
        return None
