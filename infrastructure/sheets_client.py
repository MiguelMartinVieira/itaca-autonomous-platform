"""
Cliente de Google Sheets con conexión lazy y búsqueda por GID.
"""

import logging
import time

import pandas as pd

try:
    import gspread

    HAS_GSPREAD = True
except ImportError:
    HAS_GSPREAD = False

from config.settings import CREDENTIALS_FILE
from shared.retry_utils import retry

logger = logging.getLogger(__name__)

_client = None


class SheetsClient:
    """Cliente de Google Sheets con conexión lazy y caché en memoria."""

    def __init__(self):
        self._gc = None
        self._spreadsheets_cache = {}
        self._worksheets_cache = {}

    def _ensure_connected(self):
        if self._gc is not None:
            return

        if not HAS_GSPREAD:
            raise RuntimeError("gspread no disponible. Instalar con: pip install gspread")

        if not CREDENTIALS_FILE.exists():
            raise FileNotFoundError(f"No se encontró {CREDENTIALS_FILE}")

        self._gc = gspread.service_account(filename=str(CREDENTIALS_FILE))

    def _get_spreadsheet(self, sheet_key):
        self._ensure_connected()
        if sheet_key not in self._spreadsheets_cache:
            self._spreadsheets_cache[sheet_key] = self._gc.open_by_key(sheet_key)
        return self._spreadsheets_cache[sheet_key]

    @retry(max_attempts=3, initial_delay=2.0, exceptions=(gspread.exceptions.APIError, Exception))
    def get_worksheet_by_gid(self, sheet_key, gid):
        """Abre una hoja de cálculo específica por GID (más fiable que por nombre)."""
        cache_key = (sheet_key, gid)
        if cache_key in self._worksheets_cache:
            return self._worksheets_cache[cache_key]

        sh = self._get_spreadsheet(sheet_key)
        for ws in sh.worksheets():
            if ws.id == gid:
                logger.info(f"✅ Conectado: {sh.title} → {ws.title} (GID: {gid})")
                self._worksheets_cache[cache_key] = ws
                return ws
        logger.warning(f"⚠️ No se encontró hoja con GID {gid}. Usando la primera.")
        ws_primera = sh.sheet1
        self._worksheets_cache[cache_key] = ws_primera
        return ws_primera

    def get_worksheet_by_name(self, sheet_key, name):
        """Abre una hoja de cálculo por nombre."""
        cache_key = (sheet_key, name)
        if cache_key in self._worksheets_cache:
            return self._worksheets_cache[cache_key]

        sh = self._get_spreadsheet(sheet_key)
        try:
            ws = sh.worksheet(name)
            logger.info(f"✅ Conectado: {sh.title} → {ws.title}")
            self._worksheets_cache[cache_key] = ws
            return ws
        except Exception:
            logger.warning(f"⚠️ No se encontró hoja '{name}'. Usando la primera.")
            ws_primera = sh.sheet1
            self._worksheets_cache[cache_key] = ws_primera
            return ws_primera

    def download_as_dataframe(self, worksheet):
        """Descarga todos los valores de un worksheet como DataFrame con _Sheet_Row."""
        data = worksheet.get_all_values()
        if len(data) <= 1:
            return pd.DataFrame()
        df = pd.DataFrame(data[1:], columns=data[0])
        df["_Sheet_Row"] = range(2, 2 + len(df))
        return df

    @retry(max_attempts=3, initial_delay=2.0, exceptions=(gspread.exceptions.APIError, Exception))
    def update_cells(self, worksheet, updates):
        """
        Actualiza celdas en batch en una sola llamada.
        updates: lista de tuplas (cell_address, value) ej: [("G5", True), ("G6", True)]
        """
        if not updates:
            return

        if hasattr(worksheet, "batch_update"):
            data = [{"range": cell_address, "values": [[value]]} for cell_address, value in updates]
            worksheet.batch_update(data)
        else:
            for cell_address, value in updates:
                worksheet.update(range_name=cell_address, values=[[value]])
                time.sleep(0.5)

    def batch_get(self, worksheet, ranges):
        """Lee múltiples rangos de celdas en una sola llamada."""
        return worksheet.batch_get(ranges)


def get_client():
    """Devuelve la instancia singleton del cliente de Sheets."""
    global _client
    if _client is None:
        _client = SheetsClient()
    return _client
