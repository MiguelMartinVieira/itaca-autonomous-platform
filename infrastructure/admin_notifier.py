"""
Centralizador de notificaciones al administrador.
"""

import logging

from config.settings import ADMIN_PHONE
from infrastructure.whatsapp_sender import enviar_mensaje_rapido

logger = logging.getLogger(__name__)


def enviar_resumen_ejecucion(workflow_name, resultado):
    """
    Envía un resumen estándar de la ejecución de un workflow.

    Args:
        workflow_name (str): Nombre amigable del workflow.
        resultado (dict): {enviados: int, total: int, fallidos: list}
    """
    if not isinstance(resultado, dict) or not resultado:
        return

    # Solo workflows con métricas de mensajería ('total' o 'enviados') envían resumen de envío
    if "total" not in resultado and "enviados" not in resultado:
        return

    enviados = resultado.get("enviados", 0)
    total = resultado.get("total", 0)
    fallidos = resultado.get("fallidos", [])

    if total == 0:
        msg = f"🤖 *{workflow_name}*: No había mensajes que enviar."
    elif enviados == total:
        msg = f"✅ *{workflow_name}*: {enviados}/{total} mensajes enviados correctamente."
    else:
        msg = f"⚠️ *{workflow_name}*: {enviados}/{total} enviados."
        if fallidos:
            msg += "\n\n*Fallidos:*\n" + "\n".join(f"❌ {nombre}" for nombre in fallidos)

    logger.info(f"Enviando notificación de resumen: {workflow_name}")
    enviar_mensaje_rapido(ADMIN_PHONE, msg)


def enviar_alerta_faltantes(workflow_name, faltantes):
    """
    Notifica sobre códigos de contacto que no existen en la agenda pero tienen deuda/clases.
    """
    if not faltantes:
        return

    from datetime import datetime

    fecha = datetime.now().strftime("%d/%m/%Y")

    # Total monetario solo si hay entradas con importe real
    deudas = [f.get("deuda") or 0 for f in faltantes]
    total_euros = sum(d for d in deudas if d)

    lineas = [
        f"⚠️ *Contactos faltantes — {workflow_name}*",
        f"_{fecha}_",
        "",
        "No están en contactos pero tienen actividad pendiente:",
        "",
    ]

    for f in faltantes:
        alumno = f.get("alumno", "?")
        codigo = f.get("codigo_buscado", "?")
        deuda = f.get("deuda")
        periodo = f.get("periodo", "")

        lineas.append(f"• *{codigo}* — {alumno}")
        detalle_parts = []
        if deuda:
            deuda_str = f"{deuda:.2f}€" if deuda != int(deuda) else f"{int(deuda)}€"
            detalle_parts.append(deuda_str)
        if periodo:
            detalle_parts.append(periodo)
        if detalle_parts:
            lineas.append(f"  {' · '.join(detalle_parts)}")

    # Pie: incluye importe solo si hay datos monetarios
    pie_parts = [f"{len(faltantes)} sin contactar"]
    if total_euros > 0:
        total_str = (
            f"{total_euros:.2f}€" if total_euros != int(total_euros) else f"{int(total_euros)}€"
        )
        pie_parts.append(f"{total_str} no notificados")

    lineas += ["", f"*{' · '.join(pie_parts)}*"]

    mensaje = "\n".join(lineas)
    logger.info(f"Enviando alerta de faltantes: {workflow_name} ({len(faltantes)})")
    enviar_mensaje_rapido(ADMIN_PHONE, mensaje)


def enviar_error_critico(workflow_name, error_msg):
    """Notifica un fallo catastrófico en un workflow por WhatsApp, con fallback por correo."""
    msg = f"🚨 *ERROR CRÍTICO: {workflow_name}*\n\n{error_msg}"
    logger.error(f"Enviando alerta de error crítico: {workflow_name}")
    exito_wa = False
    try:
        exito_wa = bool(enviar_mensaje_rapido(ADMIN_PHONE, msg))
    except Exception as e:
        logger.error(f"Error enviando WhatsApp de error crítico: {e}")

    if not exito_wa:
        logger.warning(
            f"Fallo envío WhatsApp de error crítico para {workflow_name}. Activando fallback por correo..."
        )
        try:
            from shared.email_utils import enviar_alerta_correo

            asunto = f"🚨 ERROR CRÍTICO Ítaca: {workflow_name}"
            cuerpo = (
                f"Se ha producido un error crítico en el workflow '{workflow_name}'.\n\n"
                f"Detalle:\n{error_msg}"
            )
            enviar_alerta_correo(asunto, cuerpo)
        except Exception as e_mail:
            logger.error(f"Error en fallback de correo para error crítico: {e_mail}")
