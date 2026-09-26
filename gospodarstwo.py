"""
Gospodarstwo – zarządzanie polami, zabiegami, zbiorami i planem prac
Dane trzymane trwale w Google Sheets (patrz README_KONFIGURACJA.md).
"""
import hmac
import math
import re
import time
from datetime import date, datetime, timedelta

import io as _io

import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
import streamlit as st
from streamlit_gsheets import GSheetsConnection
from google.oauth2.service_account import Credentials as GCredentials
from googleapiclient.discovery import build as google_build
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload

st.set_page_config(
    page_title="Moje gospodarstwo",
    page_icon="🌾",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ======================================================================
# LOGOWANIE HASŁEM
# Hasło trzymane w st.secrets (plik .streamlit/secrets.toml, NIGDY w repo GitHub).
# ======================================================================

LIMIT_NIEUDANYCH_PROB = 5
CZAS_BLOKADY_PO_PROBACH_SEKUND = 30


def haslo_poprawne(wpisane: str) -> bool:
    if not wpisane:
        return False
    oczekiwane = st.secrets.get("haslo", {}).get("haslo", "")
    if not oczekiwane:
        return False
    return hmac.compare_digest(wpisane, oczekiwane)


def ekran_logowania():
    st.title("🌾 Moje gospodarstwo")
    if "proby_logowania" not in st.session_state:
        st.session_state.proby_logowania = 0
        st.session_state.zablokowano_do = 0.0

    if time.time() < st.session_state.zablokowano_do:
        pozostalo = int(st.session_state.zablokowano_do - time.time())
        st.error(f"⏳ Zbyt wiele prób. Spróbuj ponownie za {pozostalo} s.")
        st.stop()

    with st.form("logowanie"):
        haslo = st.text_input("Hasło", type="password")
        zaloguj = st.form_submit_button("Zaloguj", type="primary", use_container_width=True)

    if zaloguj:
        if haslo_poprawne(haslo):
            st.session_state.zalogowany = True
            st.session_state.proby_logowania = 0
            st.rerun()
        else:
            st.session_state.proby_logowania += 1
            if st.session_state.proby_logowania >= LIMIT_NIEUDANYCH_PROB:
                st.session_state.zablokowano_do = time.time() + CZAS_BLOKADY_PO_PROBACH_SEKUND
                st.session_state.proby_logowania = 0
                st.error("⏳ Zbyt wiele nieudanych prób.")
            else:
                st.error("❌ Nieprawidłowe hasło.")
    st.stop()


if not st.session_state.get("zalogowany"):
    ekran_logowania()

# ======================================================================
# SŁOWNIKI / STAŁE
# ======================================================================

RODZAJE_UPRAW = [
    "Pszenica ozima", "Pszenica jara", "Żyto", "Pszenżyto",
    "Jęczmień ozimy", "Jęczmień jary", "Owies", "Rzepak ozimy",
    "Kukurydza na ziarno", "Kukurydza na kiszonkę", "Ziemniaki",
    "Burak cukrowy", "Groch", "Łubin", "Trawa/łąka", "Ugór", "Inne",
]

STATUSY_POLA = ["Zasiane", "W uprawie", "Gotowe do zbioru", "Zebrane", "Ugorowane"]
IKONY_STATUS = {
    "Zasiane": "🌱", "W uprawie": "🌿", "Gotowe do zbioru": "🌾",
    "Zebrane": "✅", "Ugorowane": "⬛",
}

TYPY_ZABIEGOW = [
    "Oprysk herbicydowy", "Oprysk fungicydowy", "Oprysk insektycydowy",
    "Oprysk regulator wzrostu", "Nawożenie mineralne", "Nawożenie organiczne",
    "Wapnowanie", "Siew", "Uprawa gleby", "Inne",
]
IKONY_TYP = {
    "Oprysk herbicydowy": "🧪", "Oprysk fungicydowy": "🧪", "Oprysk insektycydowy": "🧪",
    "Oprysk regulator wzrostu": "🧪", "Nawożenie mineralne": "💊",
    "Nawożenie organiczne": "🐄", "Wapnowanie": "⚪", "Siew": "🌱",
    "Uprawa gleby": "🚜", "Inne": "📌",
}
JEDNOSTKI_DAWKI = ["l/ha", "kg/ha", "t/ha", "szt./ha", "-"]

RODZAJE_GLEBY = ["", "Bardzo lekka", "Lekka", "Średnia", "Ciężka", "Bardzo ciężka"]

KALENDARZ_OD = date(2024, 1, 1)
KALENDARZ_DO = date(2028, 12, 31)
MIESIACE_NAZWY = ["Styczeń", "Luty", "Marzec", "Kwiecień", "Maj", "Czerwiec",
                   "Lipiec", "Sierpień", "Wrzesień", "Październik", "Listopad", "Grudzień"]


def dzis() -> date:
    return date.today()


def domyslna_data(wartosc=None) -> date:
    if isinstance(wartosc, date):
        return wartosc
    return min(max(dzis(), KALENDARZ_OD), KALENDARZ_DO)


def zl(kwota, ze_groszami=False) -> str:
    try:
        kwota = float(kwota)
    except (TypeError, ValueError):
        kwota = 0.0
    return f"{kwota:,.2f} zł".replace(",", " ") if ze_groszami else f"{kwota:,.0f} zł".replace(",", " ")


def opis_terminu(d) -> str:
    if not isinstance(d, date):
        return "brak terminu"
    roznica = (d - dzis()).days
    baza = d.strftime("%d.%m.%Y")
    if roznica == 0:
        return f"{baza} · dziś"
    if roznica == 1:
        return f"{baza} · jutro"
    if 0 < roznica <= 14:
        return f"{baza} · za {roznica} dni"
    if -14 <= roznica < 0:
        return f"{baza} · {-roznica} dni temu"
    return baza


def nowe_id(df: pd.DataFrame) -> int:
    if df.empty:
        return 1
    return int(pd.to_numeric(df["ID"], errors="coerce").fillna(0).max()) + 1


def wysokosc_tabeli(liczba_wierszy: int, wiersz_dodawania: bool = False) -> int:
    wiersze = max(liczba_wierszy, 1) + (1 if wiersz_dodawania else 0)
    return min(38 + wiersze * 35 + 3, 620)


# ======================================================================
# POŁĄCZENIE Z GOOGLE SHEETS
# ======================================================================

WORKSHEET_POLA = "Pola"
WORKSHEET_ZABIEGI = "Zabiegi"
WORKSHEET_ZBIORY = "Zbiory"

# Definicja kolumn per arkusz: (wszystkie kolumny, bool, liczbowe, daty, id)
SCHEMAT_POLA = {
    "kolumny": ["ID", "Nazwa pola", "Powierzchnia (ha)", "Rodzaj uprawy", "Odmiana",
                "Rok", "Data siewu", "Status", "Gleba", "Notatka", "Zdjęcia", "Usunięte"],
    "bool": ["Usunięte"], "liczby": ["Powierzchnia (ha)", "Rok"],
    "daty": ["Data siewu"],
}
SCHEMAT_ZABIEGI = {
    "kolumny": ["ID", "Pole", "Typ", "Środek/Nawóz", "Dawka", "Jednostka",
                "Data planowana", "Data wykonania", "Wykonano?", "Koszt (zł)", "Notatka"],
    "bool": ["Wykonano?"], "liczby": ["Dawka", "Koszt (zł)"],
    "daty": ["Data planowana", "Data wykonania"],
}
SCHEMAT_ZBIORY = {
    "kolumny": ["ID", "Pole", "Data planowana", "Data zbioru", "Plon (t/ha)",
                "Wilgotność (%)", "Cena (zł/t)", "Zrealizowane?", "Notatka"],
    "bool": ["Zrealizowane?"], "liczby": ["Plon (t/ha)", "Wilgotność (%)", "Cena (zł/t)"],
    "daty": ["Data planowana", "Data zbioru"],
}


@st.cache_resource
def pobierz_polaczenie():
    return st.connection("gsheets", type=GSheetsConnection)


# ======================================================================
# ZDJĘCIA POLA — przechowywane w Google Drive (arkusz trzyma tylko ID plików)
# Wymaga: włączonego Google Drive API w tym samym projekcie Google Cloud,
# folderu na Dysku udostępnionego kontu serwisowemu (rola: Edytor) oraz
# jego ID w secrets.toml pod [drive] folder_id.
# ======================================================================

@st.cache_resource
def pobierz_usluge_drive():
    dane_konta = {k: v for k, v in dict(st.secrets["connections"]["gsheets"]).items()
                  if k != "spreadsheet"}
    poswiadczenia = GCredentials.from_service_account_info(
        dane_konta, scopes=["https://www.googleapis.com/auth/drive"])
    return google_build("drive", "v3", credentials=poswiadczenia, cache_discovery=False)


def folder_zdjec_id() -> str:
    return st.secrets.get("drive", {}).get("folder_id", "")


def wgraj_zdjecie(plik) -> str | None:
    """Wgrywa plik ze st.file_uploader do folderu Google Drive. Zwraca ID pliku albo None przy błędzie."""
    folder_id = folder_zdjec_id()
    if not folder_id:
        st.error("Brak skonfigurowanego folderu Google Drive na zdjęcia (sekcja [drive] w secrets).")
        return None
    try:
        usluga = pobierz_usluge_drive()
        media = MediaIoBaseUpload(_io.BytesIO(plik.getvalue()), mimetype=plik.type or "image/jpeg")
        metadane = {"name": plik.name, "parents": [folder_id]}
        wynik = usluga.files().create(body=metadane, media_body=media, fields="id").execute()
        return wynik.get("id")
    except Exception as e:
        st.error(f"Nie udało się wgrać zdjęcia „{plik.name}”: {e}")
        return None


def usun_zdjecie(file_id: str):
    try:
        pobierz_usluge_drive().files().delete(fileId=file_id).execute()
    except Exception:
        pass  # plik mógł już zniknąć - nie blokujemy dalszego działania appki


@st.cache_data(ttl=3600, show_spinner=False)
def pobierz_bajty_zdjecia(file_id: str):
    try:
        usluga = pobierz_usluge_drive()
        zadanie = usluga.files().get_media(fileId=file_id)
        bufor = _io.BytesIO()
        pobieranie = MediaIoBaseDownload(bufor, zadanie)
        gotowe = False
        while not gotowe:
            _, gotowe = pobieranie.next_chunk()
        return bufor.getvalue()
    except Exception:
        return None


def lista_zdjec(wartosc) -> list:
    if not wartosc or (isinstance(wartosc, float) and math.isnan(wartosc)):
        return []
    return [i.strip() for i in str(wartosc).split(",") if i.strip()]


# ---- odporne parsowanie wartości z Google Sheets ----
PRAWDA_TEKSTY = {"TRUE", "PRAWDA", "TAK", "1", "1.0", "YES", "Y", "T", "X", "✓", "✔"}
PUSTE_TEKSTY = {"", "nan", "none", "nat", "null", "<na>"}
FORMATY_DAT = (
    "%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d",
    "%Y-%m-%d %H:%M:%S", "%d.%m.%Y %H:%M:%S", "%Y-%m-%dT%H:%M:%S",
)


def _jest_puste(v) -> bool:
    if v is None:
        return True
    try:
        if pd.isna(v):
            return True
    except (TypeError, ValueError):
        pass
    return isinstance(v, str) and v.strip().lower() in PUSTE_TEKSTY


def _na_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    if _jest_puste(v):
        return False
    if isinstance(v, (int, float)):
        return v != 0
    return str(v).strip().upper() in PRAWDA_TEKSTY


def _na_liczbe(v):
    if isinstance(v, bool):
        return float(v)
    if _jest_puste(v):
        return math.nan
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    for smiec in ("\xa0", "\u202f", " ", "zł", "zl", "PLN", "ha", "t/ha"):
        s = s.replace(smiec, "")
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".") if s.count(",") == 1 else s.replace(",", "")
    elif s.count(".") > 1:
        s = s.replace(".", "")
    try:
        return float(s)
    except ValueError:
        return math.nan


def _na_date(v):
    if _jest_puste(v):
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        if 20000 < v < 80000:  # numer seryjny daty Google Sheets
            return date(1899, 12, 30) + timedelta(days=int(v))
        return None
    s = str(v).strip().lstrip("'").strip()
    s = re.sub(r"\s*r\.?$", "", s)  # "04.12.2026 r." -> "04.12.2026"
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    if re.fullmatch(r"\d{5}([.,]\d+)?", s):
        return _na_date(_na_liczbe(s))
    for fmt in FORMATY_DAT:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    try:
        wynik = pd.to_datetime(s, dayfirst=True, errors="coerce")
        return None if pd.isna(wynik) else wynik.date()
    except Exception:
        return None


def _do_tekstu(seria: pd.Series) -> pd.Series:
    return seria.map(lambda v: "" if _jest_puste(v) else str(v).strip())


def _data_do_arkusza(d) -> str:
    """Data jako tekst 'dd.mm.rrrr r.' – arkusz nie potraktuje tego jak swojej daty i nie namiesza z formatem."""
    return f"{d:%d.%m.%Y} r." if isinstance(d, date) else ""


def wyczysc_typy(df: pd.DataFrame, schemat: dict) -> pd.DataFrame:
    df = df.copy()
    for kolumna in schemat["kolumny"]:
        if kolumna not in df.columns:
            if kolumna == "ID":
                df[kolumna] = 0
            elif kolumna in schemat["bool"]:
                df[kolumna] = False
            elif kolumna in schemat["liczby"]:
                df[kolumna] = math.nan
            elif kolumna in schemat["daty"]:
                df[kolumna] = None
            else:
                df[kolumna] = ""
    for kolumna in schemat["bool"]:
        df[kolumna] = df[kolumna].map(_na_bool).astype(bool)
    for kolumna in schemat["liczby"]:
        df[kolumna] = df[kolumna].map(_na_liczbe)
    for kolumna in schemat["daty"]:
        df[kolumna] = df[kolumna].map(_na_date).astype(object)
    df["ID"] = df["ID"].map(_na_liczbe).fillna(0).astype(int)
    tekstowe = [k for k in schemat["kolumny"]
                if k not in schemat["bool"] + schemat["liczby"] + schemat["daty"] + ["ID"]]
    for kolumna in tekstowe:
        df[kolumna] = _do_tekstu(df[kolumna])
    return df[schemat["kolumny"]]


def do_zapisu(df: pd.DataFrame, schemat: dict) -> pd.DataFrame:
    """Kopia do zapisu w arkuszu: daty jako tekst, żeby Google Sheets ich nie przeformatował."""
    df = df.copy()
    for kolumna in schemat["daty"]:
        df[kolumna] = df[kolumna].map(_data_do_arkusza)
    return df


def _czytaj_arkusz(conn, nazwa: str):
    """DataFrame, albo None gdy zakładka jeszcze nie istnieje. Inne błędy lecą dalej (nie kasujemy danych przy chwilowym błędzie API)."""
    try:
        df = conn.read(worksheet=nazwa, ttl=0)
    except Exception as e:
        opis = f"{type(e).__name__}: {e}"
        if "WorksheetNotFound" in opis:
            return None
        if "EmptyDataError" in opis or "No columns to parse" in opis:
            return pd.DataFrame()
        raise
    if df is None:
        return pd.DataFrame()
    df = df.dropna(how="all")
    df = df.loc[:, [not str(k).startswith("Unnamed") for k in df.columns]]
    return df.reset_index(drop=True)


def _zapisz_arkusz(conn, nazwa: str, df: pd.DataFrame, istnieje: bool):
    if istnieje:
        conn.update(worksheet=nazwa, data=df)
    else:
        try:
            conn.create(worksheet=nazwa, data=df)
        except Exception:
            conn.update(worksheet=nazwa, data=df)


def wczytaj_arkusz(nazwa: str, schemat: dict, seed_fn) -> pd.DataFrame:
    conn = pobierz_polaczenie()
    surowy = _czytaj_arkusz(conn, nazwa)
    if surowy is None:
        # zakładka nie istnieje jeszcze w arkuszu -> zakładamy ją z danymi przykładowymi
        seed = seed_fn()
        _zapisz_arkusz(conn, nazwa, do_zapisu(seed, schemat), istnieje=False)
        return seed
    if surowy.empty:
        return pd.DataFrame(columns=schemat["kolumny"])
    return wyczysc_typy(surowy, schemat)


def zapisz_arkusz(nazwa: str, df: pd.DataFrame, schemat: dict):
    conn = pobierz_polaczenie()
    df = przelicz_id(df)
    conn.update(worksheet=nazwa, data=do_zapisu(df, schemat))
    return df


# ======================================================================
# DANE PRZYKŁADOWE (używane tylko przy pierwszym uruchomieniu, do założenia arkuszy)
# ======================================================================

def przykladowe_pola() -> pd.DataFrame:
    dane = [
        {"ID": 1, "Nazwa pola": "Za stodołą", "Powierzchnia (ha)": 4.20,
         "Rodzaj uprawy": "Pszenica ozima", "Odmiana": "Arkadia", "Rok": 2026,
         "Data siewu": date(2025, 9, 25), "Status": "W uprawie", "Gleba": "Średnia",
         "Notatka": "", "Zdjęcia": "", "Usunięte": False},
        {"ID": 2, "Nazwa pola": "Nad rzeką", "Powierzchnia (ha)": 6.80,
         "Rodzaj uprawy": "Rzepak ozimy", "Odmiana": "DK Exception", "Rok": 2026,
         "Data siewu": date(2025, 8, 20), "Status": "Gotowe do zbioru", "Gleba": "Ciężka",
         "Notatka": "Uważać na wysoką wilgotność po deszczach", "Zdjęcia": "", "Usunięte": False},
        {"ID": 3, "Nazwa pola": "Kowalskie", "Powierzchnia (ha)": 3.10,
         "Rodzaj uprawy": "Kukurydza na ziarno", "Odmiana": "P8834", "Rok": 2026,
         "Data siewu": date(2026, 4, 25), "Status": "Zasiane", "Gleba": "Lekka",
         "Notatka": "", "Zdjęcia": "", "Usunięte": False},
        {"ID": 4, "Nazwa pola": "Przy lesie", "Powierzchnia (ha)": 2.50,
         "Rodzaj uprawy": "Ugór", "Odmiana": "", "Rok": 2026,
         "Data siewu": None, "Status": "Ugorowane", "Gleba": "Bardzo lekka",
         "Notatka": "Planowany siew jęczmienia jarego na wiosnę 2027", "Zdjęcia": "", "Usunięte": False},
    ]
    return pd.DataFrame(dane)


def przykladowe_zabiegi() -> pd.DataFrame:
    dane = [
        {"ID": 1, "Pole": "Za stodołą", "Typ": "Nawożenie mineralne", "Środek/Nawóz": "Saletra amonowa 34%",
         "Dawka": 150, "Jednostka": "kg/ha", "Data planowana": date(2026, 3, 15),
         "Data wykonania": date(2026, 3, 16), "Wykonano?": True, "Koszt (zł)": 4200, "Notatka": ""},
        {"ID": 2, "Pole": "Za stodołą", "Typ": "Oprysk fungicydowy", "Środek/Nawóz": "Prosaro 250 EC",
         "Dawka": 1.0, "Jednostka": "l/ha", "Data planowana": date(2026, 5, 10),
         "Data wykonania": None, "Wykonano?": False, "Koszt (zł)": 1800, "Notatka": "Termin zależny od pogody"},
        {"ID": 3, "Pole": "Nad rzeką", "Typ": "Oprysk herbicydowy", "Środek/Nawóz": "Butisan Star",
         "Dawka": 2.0, "Jednostka": "l/ha", "Data planowana": date(2025, 9, 5),
         "Data wykonania": date(2025, 9, 6), "Wykonano?": True, "Koszt (zł)": 3060, "Notatka": ""},
        {"ID": 4, "Pole": "Kowalskie", "Typ": "Nawożenie organiczne", "Środek/Nawóz": "Obornik",
         "Dawka": 25, "Jednostka": "t/ha", "Data planowana": date(2026, 4, 10),
         "Data wykonania": date(2026, 4, 12), "Wykonano?": True, "Koszt (zł)": 1500, "Notatka": ""},
    ]
    return pd.DataFrame(dane)


def przykladowe_zbiory() -> pd.DataFrame:
    dane = [
        {"ID": 1, "Pole": "Nad rzeką", "Data planowana": date(2026, 8, 5), "Data zbioru": None,
         "Plon (t/ha)": None, "Wilgotność (%)": None, "Cena (zł/t)": 2200,
         "Zrealizowane?": False, "Notatka": ""},
        {"ID": 2, "Pole": "Za stodołą", "Data planowana": date(2026, 8, 1), "Data zbioru": None,
         "Plon (t/ha)": None, "Wilgotność (%)": None, "Cena (zł/t)": 950,
         "Zrealizowane?": False, "Notatka": ""},
    ]
    return pd.DataFrame(dane)


def przelicz_id(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["ID"] = range(1, len(df) + 1)
    return df


if "wersja_edytorow" not in st.session_state:
    st.session_state.wersja_edytorow = 0
if "edycja" not in st.session_state:
    st.session_state.edycja = True

if "zaladowano" not in st.session_state:
    try:
        with st.spinner("Łączenie z arkuszem Google..."):
            st.session_state.pola = wczytaj_arkusz(WORKSHEET_POLA, SCHEMAT_POLA, przykladowe_pola)
            st.session_state.zabiegi = wczytaj_arkusz(WORKSHEET_ZABIEGI, SCHEMAT_ZABIEGI, przykladowe_zabiegi)
            st.session_state.zbiory = wczytaj_arkusz(WORKSHEET_ZBIORY, SCHEMAT_ZBIORY, przykladowe_zbiory)
        st.session_state.zaladowano = True
    except Exception as e:
        st.error(
            "❌ Nie udało się połączyć z arkuszem Google. Sprawdź `secrets.toml` "
            "(sekcja `[connections.gsheets]`) i czy arkusz jest udostępniony kontu serwisowemu "
            "z uprawnieniem *Edytor*. Szczegóły techniczne poniżej."
        )
        st.exception(e)
        st.stop()


def zapisz_pola(nowy: pd.DataFrame):
    nowy = przelicz_id(nowy)
    with st.spinner("Zapisywanie..."):
        zapisz_arkusz(WORKSHEET_POLA, nowy, SCHEMAT_POLA)
    st.session_state.pola = nowy


def zapisz_zabiegi(nowy: pd.DataFrame):
    nowy = przelicz_id(nowy)
    with st.spinner("Zapisywanie..."):
        zapisz_arkusz(WORKSHEET_ZABIEGI, nowy, SCHEMAT_ZABIEGI)
    st.session_state.zabiegi = nowy


def zapisz_zbiory(nowy: pd.DataFrame):
    nowy = przelicz_id(nowy)
    with st.spinner("Zapisywanie..."):
        zapisz_arkusz(WORKSHEET_ZBIORY, nowy, SCHEMAT_ZBIORY)
    st.session_state.zbiory = nowy


# ======================================================================
# KONFIGURACJA KOLUMN
# ======================================================================

KONFIG_POLA_EDYCJA = {
    "ID": st.column_config.NumberColumn(disabled=True),
    "Nazwa pola": st.column_config.TextColumn(required=True),
    "Powierzchnia (ha)": st.column_config.NumberColumn(min_value=0.0, step=0.1, format="%.2f", required=True),
    "Rodzaj uprawy": st.column_config.SelectboxColumn(options=RODZAJE_UPRAW, required=True),
    "Odmiana": st.column_config.TextColumn(),
    "Rok": st.column_config.NumberColumn(min_value=2020, max_value=2035, step=1, format="%d"),
    "Data siewu": st.column_config.DateColumn(format="DD.MM.YYYY"),
    "Status": st.column_config.SelectboxColumn(options=STATUSY_POLA, required=True),
    "Gleba": st.column_config.SelectboxColumn(options=RODZAJE_GLEBY),
    "Notatka": st.column_config.TextColumn(),
    "Zdjęcia": st.column_config.TextColumn(
        disabled=True, help="Zarządzaj zdjęciami przez formularz „Dodaj/Edytuj pole” powyżej."),
    "Usunięte": st.column_config.CheckboxColumn(default=False),
}
KONFIG_POLA_ODCZYT = {k: v for k, v in KONFIG_POLA_EDYCJA.items() if k not in ("Usunięte",)}

KONFIG_ZABIEGI_EDYCJA = {
    "ID": st.column_config.NumberColumn(disabled=True),
    "Pole": st.column_config.SelectboxColumn(required=True),
    "Typ": st.column_config.SelectboxColumn(options=TYPY_ZABIEGOW, required=True),
    "Środek/Nawóz": st.column_config.TextColumn(required=True),
    "Dawka": st.column_config.NumberColumn(min_value=0.0, step=0.1, format="%.2f"),
    "Jednostka": st.column_config.SelectboxColumn(options=JEDNOSTKI_DAWKI),
    "Data planowana": st.column_config.DateColumn(format="DD.MM.YYYY"),
    "Data wykonania": st.column_config.DateColumn(format="DD.MM.YYYY"),
    "Wykonano?": st.column_config.CheckboxColumn(default=False),
    "Koszt (zł)": st.column_config.NumberColumn(min_value=0.0, step=10.0, format="%.2f zł"),
    "Notatka": st.column_config.TextColumn(),
}

KONFIG_ZBIORY_EDYCJA = {
    "ID": st.column_config.NumberColumn(disabled=True),
    "Pole": st.column_config.SelectboxColumn(required=True),
    "Data planowana": st.column_config.DateColumn(format="DD.MM.YYYY"),
    "Data zbioru": st.column_config.DateColumn(format="DD.MM.YYYY"),
    "Plon (t/ha)": st.column_config.NumberColumn(min_value=0.0, step=0.1, format="%.2f"),
    "Wilgotność (%)": st.column_config.NumberColumn(min_value=0.0, max_value=100.0, step=0.5, format="%.1f"),
    "Cena (zł/t)": st.column_config.NumberColumn(min_value=0.0, step=10.0, format="%.2f zł"),
    "Zrealizowane?": st.column_config.CheckboxColumn(default=False),
    "Notatka": st.column_config.TextColumn(),
}


# ======================================================================
# SIDEBAR
# ======================================================================

with st.sidebar:
    st.title("🌾 Moje gospodarstwo")
    c_edycja, c_wyloguj = st.columns([2, 1])
    st.session_state.edycja = c_edycja.toggle("✏️ Tryb edycji", value=st.session_state.edycja)
    if c_wyloguj.button("🚪 Wyloguj"):
        st.session_state.zalogowany = False
        st.rerun()
    st.divider()

    df_pola_aktywne = st.session_state.pola[~st.session_state.pola["Usunięte"].fillna(False)]
    st.metric("Łączny areał", f"{df_pola_aktywne['Powierzchnia (ha)'].sum():.2f} ha")
    st.metric("Liczba pól", len(df_pola_aktywne))

    st.divider()
    st.caption("Areał wg uprawy")
    if not df_pola_aktywne.empty:
        wg_uprawy = df_pola_aktywne.groupby("Rodzaj uprawy")["Powierzchnia (ha)"].sum().sort_values(ascending=False)
        for uprawa, ha in wg_uprawy.items():
            st.caption(f"• {uprawa}: {ha:.2f} ha")

    st.divider()
    if st.button("🔄 Odśwież z arkusza", use_container_width=True):
        for klucz in ("zaladowano",):
            st.session_state.pop(klucz, None)
        st.rerun()
    st.caption("Dane są zapisywane na bieżąco w Google Sheets.")


# ======================================================================
# NAGŁÓWEK
# ======================================================================

st.title("🌾 Zarządzanie gospodarstwem")

tab1, tab2, tab3, tab4, tab5 = st.tabs(
    ["🗺️ Pola", "🧪 Zabiegi (opryski i nawozy)", "🌾 Zbiory", "📅 Kalendarz prac", "📊 Podsumowanie"]
)

edycja = st.session_state.edycja
nazwy_pol = sorted(df_pola_aktywne["Nazwa pola"].tolist())
KONFIG_ZABIEGI_EDYCJA["Pole"] = st.column_config.SelectboxColumn(options=nazwy_pol, required=True)
KONFIG_ZBIORY_EDYCJA["Pole"] = st.column_config.SelectboxColumn(options=nazwy_pol, required=True)


# ======================================================================
# ZAKŁADKA 1 — POLA
# ======================================================================

with tab1:
    st.subheader("Pola / areały")

    with st.expander("➕ Dodaj nowe pole", expanded=df_pola_aktywne.empty):
        with st.form("dodaj_pole", clear_on_submit=True):
            c1, c2, c3 = st.columns(3)
            nazwa = c1.text_input("Nazwa pola*")
            powierzchnia = c2.number_input("Powierzchnia (ha)*", min_value=0.0, step=0.1)
            rodzaj = c3.selectbox("Rodzaj uprawy*", RODZAJE_UPRAW)
            c4, c5, c6 = st.columns(3)
            odmiana = c4.text_input("Odmiana")
            rok = c5.number_input("Rok", min_value=2020, max_value=2035, value=dzis().year, step=1)
            data_siewu = c6.date_input("Data siewu", value=None, min_value=KALENDARZ_OD, max_value=KALENDARZ_DO)
            c7, c8 = st.columns(2)
            status = c7.selectbox("Status", STATUSY_POLA)
            gleba = c8.selectbox("Gleba", RODZAJE_GLEBY)
            notatka = st.text_area("Notatka", height=68)
            zdjecia_nowe = st.file_uploader("Zdjęcia pola (opcjonalnie)", type=["png", "jpg", "jpeg"],
                                             accept_multiple_files=True, key="upload_dodaj_pole")
            dodaj = st.form_submit_button("➕ Dodaj pole", type="primary", use_container_width=True)
        if dodaj:
            if not nazwa.strip():
                st.error("Podaj nazwę pola.")
            else:
                ids_zdjec = []
                if zdjecia_nowe:
                    with st.spinner("Wgrywanie zdjęć..."):
                        for plik in zdjecia_nowe:
                            fid = wgraj_zdjecie(plik)
                            if fid:
                                ids_zdjec.append(fid)
                nowy = {"ID": 0, "Nazwa pola": nazwa.strip(), "Powierzchnia (ha)": powierzchnia,
                        "Rodzaj uprawy": rodzaj, "Odmiana": odmiana.strip(), "Rok": int(rok),
                        "Data siewu": data_siewu, "Status": status, "Gleba": gleba,
                        "Notatka": notatka.strip(), "Zdjęcia": ",".join(ids_zdjec), "Usunięte": False}
                zapisz_pola(pd.concat([st.session_state.pola, pd.DataFrame([nowy])], ignore_index=True))
                st.success(f"Dodano pole „{nazwa}”.")
                st.rerun()

    if not df_pola_aktywne.empty:
        with st.expander("✏️ Edytuj lub usuń pole"):
            etykiety = {row["ID"]: f"{row['Nazwa pola']} — {row['Rodzaj uprawy']} ({row['Powierzchnia (ha)']:.2f} ha)"
                        for _, row in df_pola_aktywne.iterrows()}
            wybrane_id = st.selectbox("Wybierz pole", options=list(etykiety), format_func=lambda i: etykiety[i],
                                       key="wybor_edycji_pola")
            wiersz = st.session_state.pola[st.session_state.pola["ID"] == wybrane_id].iloc[0]
            obecne_zdjecia = lista_zdjec(wiersz["Zdjęcia"])
            with st.form("edytuj_pole"):
                c1, c2, c3 = st.columns(3)
                nazwa_e = c1.text_input("Nazwa pola*", value=wiersz["Nazwa pola"])
                powierzchnia_e = c2.number_input("Powierzchnia (ha)*", min_value=0.0, step=0.1,
                                                  value=float(wiersz["Powierzchnia (ha)"]))
                rodzaj_e = c3.selectbox("Rodzaj uprawy*", RODZAJE_UPRAW,
                                         index=RODZAJE_UPRAW.index(wiersz["Rodzaj uprawy"]) if wiersz["Rodzaj uprawy"] in RODZAJE_UPRAW else 0)
                c4, c5, c6 = st.columns(3)
                odmiana_e = c4.text_input("Odmiana", value=wiersz["Odmiana"])
                rok_e = c5.number_input("Rok", min_value=2020, max_value=2035,
                                         value=int(wiersz["Rok"]) if wiersz["Rok"] else dzis().year, step=1)
                data_siewu_e = c6.date_input("Data siewu", value=wiersz["Data siewu"],
                                              min_value=KALENDARZ_OD, max_value=KALENDARZ_DO)
                c7, c8 = st.columns(2)
                status_e = c7.selectbox("Status", STATUSY_POLA,
                                         index=STATUSY_POLA.index(wiersz["Status"]) if wiersz["Status"] in STATUSY_POLA else 0)
                gleba_e = c8.selectbox("Gleba", RODZAJE_GLEBY,
                                        index=RODZAJE_GLEBY.index(wiersz["Gleba"]) if wiersz["Gleba"] in RODZAJE_GLEBY else 0)
                notatka_e = st.text_area("Notatka", value=wiersz["Notatka"], height=68)

                flagi_usun_zdj = []
                if obecne_zdjecia:
                    st.caption("Obecne zdjęcia — zaznacz „Usuń”, żeby skasować przy zapisie:")
                    kol_zdj = st.columns(min(len(obecne_zdjecia), 4))
                    for idx, fid in enumerate(obecne_zdjecia):
                        with kol_zdj[idx % len(kol_zdj)]:
                            bajty = pobierz_bajty_zdjecia(fid)
                            if bajty:
                                st.image(bajty, use_container_width=True)
                            else:
                                st.caption("⚠️ Nie udało się wczytać zdjęcia")
                            flagi_usun_zdj.append(st.checkbox("Usuń", key=f"usun_zdj_{wybrane_id}_{fid}"))
                zdjecia_nowe_e = st.file_uploader("Dodaj nowe zdjęcia", type=["png", "jpg", "jpeg"],
                                                   accept_multiple_files=True, key=f"upload_edytuj_{wybrane_id}")

                b1, b2 = st.columns(2)
                zapisz_btn = b1.form_submit_button("💾 Zapisz zmiany", type="primary", use_container_width=True)
                usun_btn = b2.form_submit_button("🗑️ Usuń pole", use_container_width=True)
            if zapisz_btn:
                zostawione = [fid for fid, usun in zip(obecne_zdjecia, flagi_usun_zdj) if not usun]
                usuniete = [fid for fid, usun in zip(obecne_zdjecia, flagi_usun_zdj) if usun]
                nowe_id_zdjec = []
                if zdjecia_nowe_e:
                    with st.spinner("Wgrywanie zdjęć..."):
                        for plik in zdjecia_nowe_e:
                            fid = wgraj_zdjecie(plik)
                            if fid:
                                nowe_id_zdjec.append(fid)
                for fid in usuniete:
                    usun_zdjecie(fid)
                wszystkie_zdjecia = zostawione + nowe_id_zdjec

                df = st.session_state.pola.copy()
                maska = df["ID"] == wybrane_id
                df.loc[maska, ["Nazwa pola", "Powierzchnia (ha)", "Rodzaj uprawy", "Odmiana", "Rok",
                                "Data siewu", "Status", "Gleba", "Notatka", "Zdjęcia"]] = \
                    [nazwa_e.strip(), powierzchnia_e, rodzaj_e, odmiana_e.strip(), int(rok_e),
                     data_siewu_e, status_e, gleba_e, notatka_e.strip(), ",".join(wszystkie_zdjecia)]
                zapisz_pola(df)
                st.success("Zapisano zmiany.")
                st.rerun()
            if usun_btn:
                for fid in obecne_zdjecia:
                    usun_zdjecie(fid)
                df = st.session_state.pola[st.session_state.pola["ID"] != wybrane_id]
                zapisz_pola(df)
                st.success("Usunięto pole.")
                st.rerun()

    st.divider()
    st.subheader("Wszystkie pola")
    st.dataframe(
        df_pola_aktywne.drop(columns=["Usunięte", "Zdjęcia"]),
        hide_index=True, use_container_width=True,
        height=wysokosc_tabeli(len(df_pola_aktywne)),
        column_config=KONFIG_POLA_ODCZYT,
    )

    if edycja:
        with st.expander("🛠️ Zaawansowane: edytuj bezpośrednio w tabeli (zbiorczo)"):
            st.caption("Dodawaj wiersze przyciskiem „+” na dole tabeli, usuwaj zaznaczając wiersz i wciskając Delete.")
            edytowane = st.data_editor(
                st.session_state.pola,
                hide_index=True,
                use_container_width=True,
                num_rows="dynamic",
                height=wysokosc_tabeli(len(st.session_state.pola), wiersz_dodawania=True),
                column_config=KONFIG_POLA_EDYCJA,
                column_order=["Nazwa pola", "Powierzchnia (ha)", "Rodzaj uprawy", "Odmiana", "Rok",
                              "Data siewu", "Status", "Gleba", "Notatka", "Usunięte"],
                key=f"edytor_pola_{st.session_state.wersja_edytorow}",
            )
            if not edytowane.equals(st.session_state.pola):
                zapisz_pola(edytowane)
                st.rerun()

    st.divider()
    st.subheader("Karty pól")
    kolumny = st.columns(3)
    for i, (_, wiersz) in enumerate(df_pola_aktywne.iterrows()):
        with kolumny[i % 3]:
            ikona = IKONY_STATUS.get(wiersz["Status"], "🌾")
            with st.container(border=True):
                zdjecia_karty = lista_zdjec(wiersz["Zdjęcia"])
                if zdjecia_karty:
                    bajty = pobierz_bajty_zdjecia(zdjecia_karty[0])
                    if bajty:
                        st.image(bajty, use_container_width=True)
                st.markdown(f"**{ikona} {wiersz['Nazwa pola']}**")
                st.caption(f"{wiersz['Rodzaj uprawy']}" + (f" · {wiersz['Odmiana']}" if wiersz["Odmiana"] else ""))
                st.write(f"📐 {wiersz['Powierzchnia (ha)']:.2f} ha  ·  {wiersz['Status']}")
                if isinstance(wiersz["Data siewu"], date):
                    st.caption(f"🌱 Siew: {wiersz['Data siewu'].strftime('%d.%m.%Y')}")
                if wiersz["Notatka"]:
                    st.caption(f"📝 {wiersz['Notatka']}")
                if len(zdjecia_karty) > 1:
                    st.caption(f"📷 +{len(zdjecia_karty) - 1} więcej zdjęć")


# ======================================================================
# ZAKŁADKA 2 — ZABIEGI
# ======================================================================

with tab2:
    st.subheader("Opryski, nawożenie i inne zabiegi")
    if not nazwy_pol:
        st.warning("Najpierw dodaj przynajmniej jedno pole w zakładce „Pola”.")
    else:
        with st.expander("➕ Dodaj nowy zabieg", expanded=st.session_state.zabiegi.empty):
            with st.form("dodaj_zabieg", clear_on_submit=True):
                c1, c2 = st.columns(2)
                pole_z = c1.selectbox("Pole*", nazwy_pol)
                typ_z = c2.selectbox("Typ*", TYPY_ZABIEGOW)
                c3, c4, c5 = st.columns(3)
                srodek_z = c3.text_input("Środek/Nawóz*")
                dawka_z = c4.number_input("Dawka", min_value=0.0, step=0.1)
                jednostka_z = c5.selectbox("Jednostka", JEDNOSTKI_DAWKI)
                c6, c7, c8 = st.columns(3)
                data_plan_z = c6.date_input("Data planowana", value=dzis(), min_value=KALENDARZ_OD, max_value=KALENDARZ_DO)
                wykonano_z = c7.checkbox("Wykonano?")
                data_wyk_z = c8.date_input("Data wykonania", value=None, min_value=KALENDARZ_OD, max_value=KALENDARZ_DO)
                koszt_z = st.number_input("Koszt (zł)", min_value=0.0, step=10.0)
                notatka_z = st.text_area("Notatka", height=68)
                dodaj_z = st.form_submit_button("➕ Dodaj zabieg", type="primary", use_container_width=True)
            if dodaj_z:
                if not srodek_z.strip():
                    st.error("Podaj nazwę środka/nawozu.")
                else:
                    nowy = {"ID": 0, "Pole": pole_z, "Typ": typ_z, "Środek/Nawóz": srodek_z.strip(),
                            "Dawka": dawka_z, "Jednostka": jednostka_z, "Data planowana": data_plan_z,
                            "Data wykonania": data_wyk_z, "Wykonano?": wykonano_z, "Koszt (zł)": koszt_z,
                            "Notatka": notatka_z.strip()}
                    zapisz_zabiegi(pd.concat([st.session_state.zabiegi, pd.DataFrame([nowy])], ignore_index=True))
                    st.success("Dodano zabieg.")
                    st.rerun()

        if not st.session_state.zabiegi.empty:
            with st.expander("✏️ Edytuj lub usuń zabieg"):
                etykiety = {row["ID"]: f"{row['Pole']} — {row['Typ']} — {row['Środek/Nawóz']} "
                                        f"({opis_terminu(row['Data planowana'])})"
                            for _, row in st.session_state.zabiegi.iterrows()}
                wybrane_id = st.selectbox("Wybierz zabieg", options=list(etykiety), format_func=lambda i: etykiety[i],
                                           key="wybor_edycji_zabiegu")
                wiersz = st.session_state.zabiegi[st.session_state.zabiegi["ID"] == wybrane_id].iloc[0]
                with st.form("edytuj_zabieg"):
                    c1, c2 = st.columns(2)
                    pole_e = c1.selectbox("Pole*", nazwy_pol,
                                           index=nazwy_pol.index(wiersz["Pole"]) if wiersz["Pole"] in nazwy_pol else 0)
                    typ_e = c2.selectbox("Typ*", TYPY_ZABIEGOW,
                                          index=TYPY_ZABIEGOW.index(wiersz["Typ"]) if wiersz["Typ"] in TYPY_ZABIEGOW else 0)
                    c3, c4, c5 = st.columns(3)
                    srodek_e = c3.text_input("Środek/Nawóz*", value=wiersz["Środek/Nawóz"])
                    dawka_e = c4.number_input("Dawka", min_value=0.0, step=0.1, value=float(wiersz["Dawka"] or 0))
                    jednostka_e = c5.selectbox("Jednostka", JEDNOSTKI_DAWKI,
                                                index=JEDNOSTKI_DAWKI.index(wiersz["Jednostka"]) if wiersz["Jednostka"] in JEDNOSTKI_DAWKI else 0)
                    c6, c7, c8 = st.columns(3)
                    data_plan_e = c6.date_input("Data planowana", value=wiersz["Data planowana"],
                                                 min_value=KALENDARZ_OD, max_value=KALENDARZ_DO)
                    wykonano_e = c7.checkbox("Wykonano?", value=bool(wiersz["Wykonano?"]))
                    data_wyk_e = c8.date_input("Data wykonania", value=wiersz["Data wykonania"],
                                                min_value=KALENDARZ_OD, max_value=KALENDARZ_DO)
                    koszt_e = st.number_input("Koszt (zł)", min_value=0.0, step=10.0, value=float(wiersz["Koszt (zł)"] or 0))
                    notatka_e = st.text_area("Notatka", value=wiersz["Notatka"], height=68)
                    b1, b2 = st.columns(2)
                    zapisz_btn = b1.form_submit_button("💾 Zapisz zmiany", type="primary", use_container_width=True)
                    usun_btn = b2.form_submit_button("🗑️ Usuń zabieg", use_container_width=True)
                if zapisz_btn:
                    df = st.session_state.zabiegi.copy()
                    maska = df["ID"] == wybrane_id
                    df.loc[maska, ["Pole", "Typ", "Środek/Nawóz", "Dawka", "Jednostka", "Data planowana",
                                    "Data wykonania", "Wykonano?", "Koszt (zł)", "Notatka"]] = \
                        [pole_e, typ_e, srodek_e.strip(), dawka_e, jednostka_e, data_plan_e,
                         data_wyk_e, wykonano_e, koszt_e, notatka_e.strip()]
                    zapisz_zabiegi(df)
                    st.success("Zapisano zmiany.")
                    st.rerun()
                if usun_btn:
                    df = st.session_state.zabiegi[st.session_state.zabiegi["ID"] != wybrane_id]
                    zapisz_zabiegi(df)
                    st.success("Usunięto zabieg.")
                    st.rerun()

        st.divider()
        st.subheader("Wszystkie zabiegi")
        st.dataframe(
            st.session_state.zabiegi.drop(columns=["ID"]),
            hide_index=True, use_container_width=True,
            height=wysokosc_tabeli(len(st.session_state.zabiegi)),
            column_config=KONFIG_ZABIEGI_EDYCJA,
        )

        if edycja:
            with st.expander("🛠️ Zaawansowane: edytuj bezpośrednio w tabeli (zbiorczo)"):
                edytowane = st.data_editor(
                    st.session_state.zabiegi,
                    hide_index=True,
                    use_container_width=True,
                    num_rows="dynamic",
                    height=wysokosc_tabeli(len(st.session_state.zabiegi), wiersz_dodawania=True),
                    column_config=KONFIG_ZABIEGI_EDYCJA,
                    column_order=["Pole", "Typ", "Środek/Nawóz", "Dawka", "Jednostka",
                                  "Data planowana", "Data wykonania", "Wykonano?", "Koszt (zł)", "Notatka"],
                    key=f"edytor_zabiegi_{st.session_state.wersja_edytorow}",
                )
                if not edytowane.equals(st.session_state.zabiegi):
                    zapisz_zabiegi(edytowane)
                    st.rerun()

        st.divider()
        st.subheader("Zabiegi wg pola")
        for nazwa in nazwy_pol:
            grupa = st.session_state.zabiegi[st.session_state.zabiegi["Pole"] == nazwa]
            if grupa.empty:
                continue
            koszt = grupa["Koszt (zł)"].fillna(0).sum()
            do_wykonania = grupa[~grupa["Wykonano?"].fillna(False)]
            with st.expander(f"🗺️ **{nazwa}** · {len(grupa)} zabiegów · koszt {zl(koszt, True)}"
                              + (f" · ⏳ {len(do_wykonania)} do wykonania" if len(do_wykonania) else " · ✅ wszystko wykonane")):
                for _, w in grupa.sort_values("Data planowana").iterrows():
                    ikona = IKONY_TYP.get(w["Typ"], "📌")
                    stan = "✅" if w["Wykonano?"] else "⏳"
                    st.markdown(
                        f"{stan} {ikona} **{w['Typ']}** — {w['Środek/Nawóz']} "
                        f"({w['Dawka']} {w['Jednostka']}) · {opis_terminu(w['Data planowana'])} · {zl(w['Koszt (zł)'], True)}"
                    )


# ======================================================================
# ZAKŁADKA 3 — ZBIORY
# ======================================================================

with tab3:
    st.subheader("Zbiory — planowane i zrealizowane")
    if not nazwy_pol:
        st.warning("Najpierw dodaj przynajmniej jedno pole w zakładce „Pola”.")
    else:
        with st.expander("➕ Dodaj nowy zbiór", expanded=st.session_state.zbiory.empty):
            with st.form("dodaj_zbior", clear_on_submit=True):
                c1, c2 = st.columns(2)
                pole_zb = c1.selectbox("Pole*", nazwy_pol)
                data_plan_zb = c2.date_input("Data planowana", value=dzis(), min_value=KALENDARZ_OD, max_value=KALENDARZ_DO)
                c3, c4 = st.columns(2)
                zrealizowane_zb = c3.checkbox("Zrealizowane?")
                data_zbioru_zb = c4.date_input("Data zbioru", value=None, min_value=KALENDARZ_OD, max_value=KALENDARZ_DO)
                c5, c6, c7 = st.columns(3)
                plon_zb = c5.number_input("Plon (t/ha)", min_value=0.0, step=0.1)
                wilgotnosc_zb = c6.number_input("Wilgotność (%)", min_value=0.0, max_value=100.0, step=0.5)
                cena_zb = c7.number_input("Cena (zł/t)", min_value=0.0, step=10.0)
                notatka_zb = st.text_area("Notatka", height=68)
                dodaj_zb = st.form_submit_button("➕ Dodaj zbiór", type="primary", use_container_width=True)
            if dodaj_zb:
                nowy = {"ID": 0, "Pole": pole_zb, "Data planowana": data_plan_zb, "Data zbioru": data_zbioru_zb,
                        "Plon (t/ha)": plon_zb, "Wilgotność (%)": wilgotnosc_zb, "Cena (zł/t)": cena_zb,
                        "Zrealizowane?": zrealizowane_zb, "Notatka": notatka_zb.strip()}
                zapisz_zbiory(pd.concat([st.session_state.zbiory, pd.DataFrame([nowy])], ignore_index=True))
                st.success("Dodano zbiór.")
                st.rerun()

        if not st.session_state.zbiory.empty:
            with st.expander("✏️ Edytuj lub usuń zbiór"):
                etykiety = {row["ID"]: f"{row['Pole']} — zbiór {opis_terminu(row['Data planowana'])}"
                            for _, row in st.session_state.zbiory.iterrows()}
                wybrane_id = st.selectbox("Wybierz zbiór", options=list(etykiety), format_func=lambda i: etykiety[i],
                                           key="wybor_edycji_zbioru")
                wiersz = st.session_state.zbiory[st.session_state.zbiory["ID"] == wybrane_id].iloc[0]
                with st.form("edytuj_zbior"):
                    c1, c2 = st.columns(2)
                    pole_e = c1.selectbox("Pole*", nazwy_pol,
                                           index=nazwy_pol.index(wiersz["Pole"]) if wiersz["Pole"] in nazwy_pol else 0)
                    data_plan_e = c2.date_input("Data planowana", value=wiersz["Data planowana"],
                                                 min_value=KALENDARZ_OD, max_value=KALENDARZ_DO)
                    c3, c4 = st.columns(2)
                    zrealizowane_e = c3.checkbox("Zrealizowane?", value=bool(wiersz["Zrealizowane?"]))
                    data_zbioru_e = c4.date_input("Data zbioru", value=wiersz["Data zbioru"],
                                                   min_value=KALENDARZ_OD, max_value=KALENDARZ_DO)
                    c5, c6, c7 = st.columns(3)
                    plon_e = c5.number_input("Plon (t/ha)", min_value=0.0, step=0.1, value=float(wiersz["Plon (t/ha)"] or 0))
                    wilgotnosc_e = c6.number_input("Wilgotność (%)", min_value=0.0, max_value=100.0, step=0.5,
                                                    value=float(wiersz["Wilgotność (%)"] or 0))
                    cena_e = c7.number_input("Cena (zł/t)", min_value=0.0, step=10.0, value=float(wiersz["Cena (zł/t)"] or 0))
                    notatka_e = st.text_area("Notatka", value=wiersz["Notatka"], height=68)
                    b1, b2 = st.columns(2)
                    zapisz_btn = b1.form_submit_button("💾 Zapisz zmiany", type="primary", use_container_width=True)
                    usun_btn = b2.form_submit_button("🗑️ Usuń zbiór", use_container_width=True)
                if zapisz_btn:
                    df = st.session_state.zbiory.copy()
                    maska = df["ID"] == wybrane_id
                    df.loc[maska, ["Pole", "Data planowana", "Data zbioru", "Plon (t/ha)", "Wilgotność (%)",
                                    "Cena (zł/t)", "Zrealizowane?", "Notatka"]] = \
                        [pole_e, data_plan_e, data_zbioru_e, plon_e, wilgotnosc_e, cena_e, zrealizowane_e, notatka_e.strip()]
                    zapisz_zbiory(df)
                    st.success("Zapisano zmiany.")
                    st.rerun()
                if usun_btn:
                    df = st.session_state.zbiory[st.session_state.zbiory["ID"] != wybrane_id]
                    zapisz_zbiory(df)
                    st.success("Usunięto zbiór.")
                    st.rerun()

        st.divider()
        st.subheader("Wszystkie zbiory")
        st.dataframe(
            st.session_state.zbiory.drop(columns=["ID"]),
            hide_index=True, use_container_width=True,
            height=wysokosc_tabeli(len(st.session_state.zbiory)),
            column_config=KONFIG_ZBIORY_EDYCJA,
        )

        if edycja:
            with st.expander("🛠️ Zaawansowane: edytuj bezpośrednio w tabeli (zbiorczo)"):
                edytowane = st.data_editor(
                    st.session_state.zbiory,
                    hide_index=True,
                    use_container_width=True,
                    num_rows="dynamic",
                    height=wysokosc_tabeli(len(st.session_state.zbiory), wiersz_dodawania=True),
                    column_config=KONFIG_ZBIORY_EDYCJA,
                    column_order=["Pole", "Data planowana", "Data zbioru", "Plon (t/ha)",
                                  "Wilgotność (%)", "Cena (zł/t)", "Zrealizowane?", "Notatka"],
                    key=f"edytor_zbiory_{st.session_state.wersja_edytorow}",
                )
                if not edytowane.equals(st.session_state.zbiory):
                    zapisz_zbiory(edytowane)
                    st.rerun()

        st.divider()
        st.subheader("Plon i przychód")
        pow_mapa = df_pola_aktywne.set_index("Nazwa pola")["Powierzchnia (ha)"].to_dict()
        wiersze = []
        for _, w in st.session_state.zbiory.iterrows():
            powierzchnia = pow_mapa.get(w["Pole"], 0.0)
            plon_t_ha = w["Plon (t/ha)"] if pd.notna(w["Plon (t/ha)"]) else None
            cena = w["Cena (zł/t)"] if pd.notna(w["Cena (zł/t)"]) else 0
            # Przychód z 1 ha - niezależny od wielkości pola, do porównania opłacalności upraw
            przychod_z_ha = plon_t_ha * cena if plon_t_ha is not None else None
            plon_total = plon_t_ha * powierzchnia if plon_t_ha is not None else None
            przychod_calkowity = przychod_z_ha * powierzchnia if przychod_z_ha is not None else None
            wiersze.append({
                "Pole": w["Pole"], "Powierzchnia (ha)": powierzchnia,
                "Plon (t/ha)": plon_t_ha, "Cena (zł/t)": cena,
                "Przychód z 1 ha (zł/ha)": przychod_z_ha,
                "Plon łącznie (t)": plon_total,
                "Przychód całkowity (zł)": przychod_calkowity,
                "Zrealizowane?": bool(w["Zrealizowane?"]),
            })
        tabela_plonow = pd.DataFrame(wiersze)
        if tabela_plonow.empty:
            st.info("Brak danych o zbiorach.")
        else:
            st.dataframe(
                tabela_plonow, hide_index=True, use_container_width=True,
                height=wysokosc_tabeli(len(tabela_plonow)),
                column_order=["Pole", "Powierzchnia (ha)", "Plon (t/ha)", "Cena (zł/t)",
                              "Przychód z 1 ha (zł/ha)", "Plon łącznie (t)", "Przychód całkowity (zł)",
                              "Zrealizowane?"],
                column_config={
                    "Powierzchnia (ha)": st.column_config.NumberColumn(format="%.2f"),
                    "Plon (t/ha)": st.column_config.NumberColumn(format="%.2f"),
                    "Cena (zł/t)": st.column_config.NumberColumn(format="%.2f zł"),
                    "Przychód z 1 ha (zł/ha)": st.column_config.NumberColumn(
                        format="%.2f zł", help="Plon (t/ha) × Cena (zł/t) — przychód planowany lub uzyskany z 1 ha, niezależnie od wielkości pola."),
                    "Plon łącznie (t)": st.column_config.NumberColumn(format="%.2f"),
                    "Przychód całkowity (zł)": st.column_config.NumberColumn(format="%.2f zł"),
                    "Zrealizowane?": st.column_config.CheckboxColumn("Zrealizowane?"),
                },
            )
            m1, m2 = st.columns(2)
            zrealizowane = tabela_plonow[tabela_plonow["Zrealizowane?"] == True]
            planowane = tabela_plonow[tabela_plonow["Zrealizowane?"] == False]
            m1.metric("💰 Przychód ze zrealizowanych zbiorów",
                      zl(zrealizowane["Przychód całkowity (zł)"].fillna(0).sum(), True))
            m2.metric("📌 Przychód planowany (jeszcze niezrealizowany)",
                      zl(planowane["Przychód całkowity (zł)"].fillna(0).sum(), True))


# ======================================================================
# ZAKŁADKA 4 — KALENDARZ PRAC
# ======================================================================

with tab4:
    st.subheader("Nadchodzące i zaległe prace")

    wpisy = []
    for _, w in st.session_state.zabiegi.iterrows():
        if w["Wykonano?"]:
            continue
        if pd.notna(w["Data planowana"]):
            wpisy.append({"Data": w["Data planowana"], "Pole": w["Pole"],
                          "Co": f"{IKONY_TYP.get(w['Typ'], '📌')} {w['Typ']} — {w['Środek/Nawóz']}",
                          "Rodzaj": "Zabieg"})
    for _, w in st.session_state.zbiory.iterrows():
        if w["Zrealizowane?"]:
            continue
        if pd.notna(w["Data planowana"]):
            wpisy.append({"Data": w["Data planowana"], "Pole": w["Pole"],
                          "Co": "🌾 Zbiór", "Rodzaj": "Zbiór"})

    df_plan = pd.DataFrame(wpisy)
    if df_plan.empty:
        st.info("Brak zaplanowanych, niewykonanych prac. 🎉")
    else:
        df_plan = df_plan.sort_values("Data")
        zalegle = df_plan[df_plan["Data"] < dzis()]
        if not zalegle.empty:
            st.error(f"⚠️ {len(zalegle)} zaległych zadań")
            for _, w in zalegle.iterrows():
                st.markdown(f"- **{w['Pole']}** — {w['Co']} · {opis_terminu(w['Data'])}")
            st.divider()

        nadchodzace = df_plan[df_plan["Data"] >= dzis()]
        ostatni_miesiac = None
        for _, w in nadchodzace.iterrows():
            miesiac = (w["Data"].year, w["Data"].month)
            if miesiac != ostatni_miesiac:
                st.markdown(f"#### 🗓️ {MIESIACE_NAZWY[miesiac[1] - 1]} {miesiac[0]}")
                ostatni_miesiac = miesiac
            st.markdown(f"- **{w['Pole']}** — {w['Co']} · {opis_terminu(w['Data'])}")

    st.divider()
    st.subheader("Wszystkie prace na osi czasu")
    wszystkie = []
    for _, w in st.session_state.zabiegi.iterrows():
        if pd.notna(w["Data planowana"]):
            wszystkie.append({"Pole": w["Pole"], "Start": w["Data planowana"],
                              "Koniec": w["Data planowana"] + timedelta(days=1),
                              "Zadanie": f"{w['Typ']} — {w['Środek/Nawóz']}",
                              "Status": "Wykonano" if w["Wykonano?"] else "Planowane"})
    for _, w in st.session_state.zbiory.iterrows():
        if pd.notna(w["Data planowana"]):
            wszystkie.append({"Pole": w["Pole"], "Start": w["Data planowana"],
                              "Koniec": w["Data planowana"] + timedelta(days=1),
                              "Zadanie": "Zbiór",
                              "Status": "Wykonano" if w["Zrealizowane?"] else "Planowane"})
    if wszystkie:
        df_gantt = pd.DataFrame(wszystkie)
        fig = px.timeline(
            df_gantt, x_start="Start", x_end="Koniec", y="Pole", color="Status",
            hover_name="Zadanie",
            color_discrete_map={"Wykonano": "#2e7d32", "Planowane": "#f9a825"},
        )
        fig.update_yaxes(autorange="reversed")
        fig.update_layout(height=max(250, 60 * df_gantt["Pole"].nunique()), margin=dict(l=10, r=10, t=10, b=10))
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("Dodaj zabiegi lub zbiory z datą, żeby zobaczyć oś czasu.")


# ======================================================================
# ZAKŁADKA 5 — PODSUMOWANIE
# ======================================================================

with tab5:
    st.subheader("Podsumowanie finansowe i przegląd upraw")

    koszt_zabiegow = st.session_state.zabiegi["Koszt (zł)"].fillna(0).sum()
    _tabela_plonow = globals().get("tabela_plonow")
    if _tabela_plonow is not None and not _tabela_plonow.empty:
        przychod_zbiorow = _tabela_plonow.loc[
            _tabela_plonow["Zrealizowane?"] == True, "Przychód całkowity (zł)"
        ].fillna(0).sum()
    else:
        przychod_zbiorow = 0
    bilans = przychod_zbiorow - koszt_zabiegow

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("📐 Łączny areał", f"{df_pola_aktywne['Powierzchnia (ha)'].sum():.2f} ha")
    m2.metric("🧪 Koszty zabiegów", zl(koszt_zabiegow))
    m3.metric("💰 Przychód ze zbiorów", zl(przychod_zbiorow))
    m4.metric("📈 Bilans", zl(bilans), delta="na plusie" if bilans >= 0 else "na minusie",
              delta_color="normal" if bilans >= 0 else "inverse")

    col1, col2 = st.columns(2)
    with col1:
        st.markdown("##### Areał wg rodzaju uprawy")
        if not df_pola_aktywne.empty:
            wg_uprawy = df_pola_aktywne.groupby("Rodzaj uprawy")["Powierzchnia (ha)"].sum().reset_index()
            fig1 = px.pie(wg_uprawy, names="Rodzaj uprawy", values="Powierzchnia (ha)", hole=0.4)
            fig1.update_layout(margin=dict(l=10, r=10, t=10, b=10), height=350)
            st.plotly_chart(fig1, use_container_width=True)
    with col2:
        st.markdown("##### Koszty zabiegów wg pola")
        if not st.session_state.zabiegi.empty:
            wg_pola = st.session_state.zabiegi.groupby("Pole")["Koszt (zł)"].sum().reset_index().sort_values("Koszt (zł)")
            fig2 = go.Figure(go.Bar(x=wg_pola["Koszt (zł)"], y=wg_pola["Pole"], orientation="h",
                                     marker_color="#c62828"))
            fig2.update_layout(margin=dict(l=10, r=10, t=10, b=10), height=350, xaxis_title="zł")
            st.plotly_chart(fig2, use_container_width=True)

    st.markdown("##### Koszty zabiegów wg typu")
    if not st.session_state.zabiegi.empty:
        wg_typu = st.session_state.zabiegi.groupby("Typ")["Koszt (zł)"].sum().reset_index().sort_values("Koszt (zł)", ascending=False)
        fig3 = go.Figure(go.Bar(x=wg_typu["Typ"], y=wg_typu["Koszt (zł)"], marker_color="#1565c0"))
        fig3.update_layout(margin=dict(l=10, r=10, t=30, b=10), height=350, yaxis_title="zł")
        st.plotly_chart(fig3, use_container_width=True)

    st.markdown("##### Status pól")
    if not df_pola_aktywne.empty:
        wg_statusu = df_pola_aktywne["Status"].value_counts().reset_index()
        wg_statusu.columns = ["Status", "Liczba pól"]
        st.dataframe(wg_statusu, hide_index=True, use_container_width=True)
