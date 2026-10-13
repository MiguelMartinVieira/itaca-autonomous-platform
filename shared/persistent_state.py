"""
Manejo robusto de estado persistente en JSON con validación y recuperación.
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class PersistentState:
    """
    Almacena y recupera estado entre ejecuciones de forma robusta.

    Características:
    - Validación automática de corrupción de JSON
    - Respaldo automático antes de escribir
    - Recuperación de corrupción
    - Interfaz tipo diccionario
    """

    def __init__(self, file_path: Path, auto_backup: bool = True):
        """
        Args:
            file_path: Ruta del archivo JSON
            auto_backup: Si crear respaldos antes de escribir
        """
        self.file_path = Path(file_path)
        self.auto_backup = auto_backup
        self.backup_path = self.file_path.with_suffix(".json.bak")
        self._data: dict = {}
        self._load()

    def _load(self) -> None:
        """Carga el estado desde archivo, con recuperación de corrupción."""
        if not self.file_path.exists():
            logger.debug(f"Archivo de estado no existe: {self.file_path}")
            self._data = {}
            return

        try:
            content = self.file_path.read_text()
            self._data = json.loads(content)
            logger.debug(f"Estado cargado desde {self.file_path}")
        except json.JSONDecodeError as e:
            logger.warning(f"JSON corrupto en {self.file_path}: {e}")
            self._recover_from_backup()
        except Exception as e:
            logger.error(f"Error cargando estado: {e}")
            self._data = {}

    def _recover_from_backup(self) -> None:
        """Intenta recuperarse desde el respaldo."""
        if self.backup_path.exists():
            try:
                content = self.backup_path.read_text()
                self._data = json.loads(content)
                logger.info(f"Estado recuperado desde backup: {self.backup_path}")
            except Exception as e:
                logger.error(f"No se pudo recuperar desde backup: {e}")
                self._data = {}
        else:
            logger.warning("No hay backup disponible")
            self._data = {}

    def save(self) -> bool:
        """
        Guarda el estado en archivo.

        Returns:
            True si la operación fue exitosa
        """
        try:
            # Crear respaldo si el archivo existe
            if self.auto_backup and self.file_path.exists():
                try:
                    content = self.file_path.read_text()
                    self.backup_path.write_text(content)
                except Exception as e:
                    logger.warning(f"No se pudo crear backup: {e}")

            # Crear directorio si no existe
            self.file_path.parent.mkdir(parents=True, exist_ok=True)

            # Guardar con timestamp
            data_with_timestamp = {
                **self._data,
                "_updated_at": datetime.now().isoformat(timespec="seconds"),
            }

            self.file_path.write_text(json.dumps(data_with_timestamp, indent=2))
            logger.debug(f"Estado guardado en {self.file_path}")
            return True
        except Exception as e:
            logger.error(f"Error guardando estado: {e}")
            return False

    def get(self, key: str, default: Any = None) -> Any:
        """Obtiene un valor del estado."""
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        """Establece un valor en el estado."""
        self._data[key] = value

    def update(self, updates: dict) -> None:
        """Actualiza múltiples valores."""
        self._data.update(updates)

    def clear(self) -> None:
        """Limpia todo el estado."""
        self._data = {}

    def delete(self, key: str) -> bool:
        """Elimina una clave. Retorna True si existía."""
        if key in self._data:
            del self._data[key]
            return True
        return False

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self._data[key] = value

    def __contains__(self, key: str) -> bool:
        return key in self._data

    def __repr__(self) -> str:
        return f"PersistentState({self.file_path}, {len(self._data)} keys)"
