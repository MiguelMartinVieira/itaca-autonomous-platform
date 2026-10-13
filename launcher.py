#!/usr/bin/env python3
"""
Launcher unificado para los workflows de WhatsApp Automation.

Uso:
    python launcher.py formularios [--auto] [--skip-update]
    python launcher.py profesores  [--auto] [--skip-update]
    python launcher.py padres      [--auto] [--skip-update]
"""

import argparse
import importlib
import sys
from pathlib import Path

# Asegurar que el directorio raíz está en sys.path
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

# Imports tras sys.path.insert — noqa para que flake8 no se queje de E402.
import logging  # noqa: E402

import requests  # noqa: E402

from config.settings import (  # noqa: E402
    DATA_DIR,
    LOGS_DIR,
    WHATSAPP_SEND_MODE,
    WHATSAPP_SERVICE_URL,
)
from infrastructure.admin_notifier import (  # noqa: E402
    enviar_error_critico,
    enviar_resumen_ejecucion,
)
from infrastructure.data_updater import ejecutar_limpieza  # noqa: E402
from shared.logging_config import setup_logging  # noqa: E402

logger = logging.getLogger("launcher")

WORKFLOWS = {
    "formularios": ("workflows.formularios", "Recordatorio Formularios"),
    "nudge_formularios": ("workflows.nudge_formularios", "Nudge Formularios (Domingo)"),
    "profesores": ("workflows.profesores", "Pagos Profesores"),
    "padres": ("workflows.padres", "Pagos Padres"),
    "inicio_curso": ("workflows.inicio_curso", "Mensaje Inicio de Curso (Septiembre)"),
    "interceptor": ("workflows.interceptor", "Interceptor Cancelaciones"),
    "revisor_contactos": ("workflows.revisor_contactos", "Revisor de Contactos"),
    "auditor_bbdd": ("workflows.auditor_bbdd", "Auditor de Integridad"),
    "auditor": ("workflows.auditor_bbdd", "Auditor de Integridad"),
    "watchdog": ("workflows.whatsapp_watchdog", "Watchdog de WhatsApp"),
    "backup": ("workflows.backup", "Backup Base de Datos SQLite"),
    "hawala": ("workflows.hawala", "Liquidación Terceros (Hawala)"),
    "status": (None, "Estado del Servicio"),
}


def build_parser() -> argparse.ArgumentParser:
    """Construye el parser de argumentos con subparsers tipados por workflow."""
    parser = argparse.ArgumentParser(
        description="WhatsApp Automation Launcher - Ítaca",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Workflows disponibles:
  formularios        Recordar a profesores que registren clases pendientes (viernes)
  nudge_formularios  Nudge corto a profes que no respondieron al recordatorio (domingo)
  profesores         Enviar recordatorios de comisiones a profesores
  padres             Enviar recordatorios de pago a padres/alumnos
  inicio_curso       Enviar mensaje masivo de inicio de curso a padres (Septiembre)
  interceptor        Detectar cancelaciones/cambios en mensajes de WhatsApp
  revisor_contactos  Verificar si faltan contactos en la agenda (A, MA, PA para alumnos, P para profesores)
  auditor / auditor_bbdd  Auditar salud, fórmulas y reglas de negocio en BBDD de Google Sheets
  watchdog           Verificar salud de WhatsApp y alertar si está desconectado
  backup             Copia de seguridad y rotación de la base de datos SQLite (messages.db)
  hawala             Liquidación a terceros (Hawala) sin tocar Google Sheets
  status             Verificar el estado de la API de WhatsApp y estadísticas
        """,
    )

    common_parent = argparse.ArgumentParser(add_help=False)
    common_parent.add_argument(
        "--auto",
        action="store_true",
        help="Modo automático (sin confirmaciones)",
    )
    common_parent.add_argument(
        "--dry-run",
        action="store_true",
        help="Modo prueba / simulación (no envía mensajes ni muta nada)",
    )
    common_parent.add_argument(
        "--skip-update",
        action="store_true",
        help="Omitir descarga de CSVs (usar datos locales)",
    )

    subparsers = parser.add_subparsers(
        dest="workflow",
        required=True,
        help="Workflow a ejecutar",
    )

    # Workflows estándar (heredan --auto y --skip-update)
    for wf in [
        "formularios",
        "nudge_formularios",
        "padres",
        "inicio_curso",
        "interceptor",
        "revisor_contactos",
        "auditor_bbdd",
        "auditor",
        "watchdog",
        "backup",
    ]:
        subparsers.add_parser(wf, parents=[common_parent], help=WORKFLOWS[wf][1])

    # Profesores: añade --modo
    p_prof = subparsers.add_parser(
        "profesores", parents=[common_parent], help=WORKFLOWS["profesores"][1]
    )
    p_prof.add_argument(
        "--modo",
        default=None,
        help="Modo de ejecución especial (ej: bizum_semanal)",
    )

    # Hawala: liquidación y asignaciones con filtros específicos
    p_hawala = subparsers.add_parser("hawala", parents=[common_parent], help=WORKFLOWS["hawala"][1])
    p_hawala.add_argument(
        "--cantidad",
        type=float,
        default=None,
        help="Importe objetivo para hawala (ej: 200)",
    )
    p_hawala.add_argument(
        "--destinatario",
        default=None,
        help="Nombre del destinatario para hawala (ej: 'Xabier Martinez')",
    )
    p_hawala.add_argument(
        "--telefono-destinatario",
        default=None,
        help="Teléfono Bizum del destinatario para hawala (ej: '625 89 02 05')",
    )
    p_hawala.add_argument(
        "--excluir",
        default=None,
        help="Lista de PCODEs a excluir separados por comas (ej: 'P152,P028,P171')",
    )
    p_hawala.add_argument(
        "--priorizar",
        default=None,
        help="Lista de PCODEs a priorizar separados por comas (ej: 'P192,P201')",
    )
    p_hawala.add_argument(
        "--destinatarios",
        default=None,
        help="Lista de destinatarios en formato 'Nombre:Teléfono:Cantidad,...' o JSON",
    )
    p_hawala.add_argument(
        "--incluir-resto",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Enviar también el resto de mensajes de profesores para evitar solapamientos (por defecto: True)",
    )
    p_hawala.add_argument(
        "--sin-resto",
        dest="incluir_resto",
        action="store_false",
        help="Omitir el resto de profesores (procesar exclusivamente Hawala)",
    )
    p_hawala.add_argument(
        "--personal-miguel",
        dest="usar_telefono_personal_miguel",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Usar teléfono personal de Miguel (+34 644 36 53 91) para cobros de Ítaca en el resto (por defecto: True)",
    )
    p_hawala.add_argument(
        "--receptor-efectivo",
        default=None,
        help="Nombre de la persona que recibe el efectivo de los cobros Hawala (por defecto: 'Pati')",
    )

    # Status: verificación de salud y estadísticas
    subparsers.add_parser("status", parents=[common_parent], help=WORKFLOWS["status"][1])

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    # Asegurar directorios
    LOGS_DIR.mkdir(exist_ok=True)
    DATA_DIR.mkdir(exist_ok=True)

    workflow_key = args.workflow
    setup_logging(workflow_key)

    if workflow_key == "status":
        check_status()
        return

    module_path, display_name = WORKFLOWS[workflow_key]
    es_dry_run = getattr(args, "dry_run", False)

    # Validar entorno: el servicio de WhatsApp debe estar arriba si el modo es API (salvo en dry-run)
    if (
        WHATSAPP_SEND_MODE == "api"
        and workflow_key not in ("watchdog", "backup")
        and not es_dry_run
    ):
        if not validar_servicio_whatsapp():
            logger.error("❌ El servicio de WhatsApp no responde. Abortando.")
            sys.exit(1)

    # Importar workflow
    workflow = importlib.import_module(module_path)

    # Descargar solo los CSVs que necesita este workflow
    if not getattr(args, "skip_update", False):
        if not ejecutar_limpieza(solo=getattr(workflow, "CSVS_NEEDED", [])):
            logger.warning("⚠️ Error actualizando datos. Continuando con copias locales...")

    # Ejecutar workflow
    resultado = None
    try:
        import inspect

        run_params = inspect.signature(workflow.run).parameters
        kwargs = {}
        if "auto" in run_params:
            kwargs["auto"] = getattr(args, "auto", False)
        if "dry_run" in run_params and es_dry_run:
            kwargs["dry_run"] = True
        if "modo" in run_params and getattr(args, "modo", None) is not None:
            kwargs["modo"] = args.modo
        if "cantidad" in run_params and getattr(args, "cantidad", None) is not None:
            kwargs["cantidad"] = args.cantidad
        if "destinatario" in run_params and getattr(args, "destinatario", None) is not None:
            kwargs["destinatario"] = args.destinatario
        if (
            "telefono_destinatario" in run_params
            and getattr(args, "telefono_destinatario", None) is not None
        ):
            kwargs["telefono_destinatario"] = args.telefono_destinatario
        if "destinatarios" in run_params and getattr(args, "destinatarios", None) is not None:
            kwargs["destinatarios"] = args.destinatarios
        if "incluir_resto" in run_params and getattr(args, "incluir_resto", None) is not None:
            kwargs["incluir_resto"] = args.incluir_resto
        if "excluir" in run_params and getattr(args, "excluir", None) is not None:
            kwargs["excluir"] = args.excluir
        if "priorizar" in run_params and getattr(args, "priorizar", None) is not None:
            kwargs["priorizar"] = args.priorizar
        if (
            "usar_telefono_personal_miguel" in run_params
            and getattr(args, "usar_telefono_personal_miguel", None) is not None
        ):
            kwargs["usar_telefono_personal_miguel"] = args.usar_telefono_personal_miguel
        if (
            "receptor_efectivo" in run_params
            and getattr(args, "receptor_efectivo", None) is not None
        ):
            kwargs["receptor_efectivo"] = args.receptor_efectivo
        resultado = workflow.run(**kwargs)
    except Exception as e:
        logger.exception(f"❌ Fallo crítico en el workflow {display_name}: {e}")
        try:
            enviar_error_critico(display_name, str(e))
        except Exception as alert_err:
            logger.error(f"No se pudo enviar alerta de error crítico: {alert_err}")
    finally:
        if (
            not es_dry_run
            and isinstance(resultado, dict)
            and ("total" in resultado or "enviados" in resultado)
        ):
            enviar_resumen_ejecucion(display_name, resultado)


def validar_servicio_whatsapp():
    """Verifica que el servicio Node.js esté respondiendo."""
    try:
        resp = requests.get(f"{WHATSAPP_SERVICE_URL}/api/health", timeout=5)
        return resp.status_code == 200
    except Exception:
        return False


def check_status():
    """Consulta y muestra las estadísticas del servicio WhatsApp."""
    print(f"\n🔍 Verificando estado en {WHATSAPP_SERVICE_URL}...")
    try:
        # Health
        h_resp = requests.get(f"{WHATSAPP_SERVICE_URL}/api/health", timeout=5)
        h_data = h_resp.json()

        # Stats
        s_resp = requests.get(f"{WHATSAPP_SERVICE_URL}/api/stats", timeout=5)
        s_data = s_resp.json()

        status = str(h_data.get("status", "")).lower()
        status_icon = "✅" if status == "connected" else "❌"

        print(f"\n{status_icon} *ESTADO:* {status.upper()}")
        print(f"📱 *Cliente:* {h_data.get('client', '?')}")

        if "stats" in s_data:
            st = s_data["stats"]
            print("\n📊 *ESTADÍSTICAS:*")
            print(f"   - Mensajes recibidos: {st.get('received', 0)}")
            print(f"   - Mensajes enviados:  {st.get('sent', 0)}")
            print(f"   - Pendientes de procesar: {st.get('unprocessed', 0)}")

        print("\n" + "=" * 50)
    except Exception as e:
        print(f"❌ No se pudo obtener el estado del servicio: {e}")


if __name__ == "__main__":
    main()
