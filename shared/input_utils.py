"""
Utilidades para input interactivo seguro en modo automático.
"""


def pedir_confirmacion(prompt: str, auto_mode: bool = False, default: bool = True):
    """
    Pide confirmación sí/no de forma segura.

    Args:
        prompt: Texto del prompt
        auto_mode: Si True, devuelve default sin bloquear
        default: Valor por defecto

    Returns:
        bool: True si sí, False si no
    """
    if auto_mode:
        print(f"{prompt} [AUTOMÁTICO: {'Sí' if default else 'No'}]")
        return default

    respuesta = input(f"{prompt} (s/n): ").strip().lower()
    if respuesta in ["s", "si", "yes", "y"]:
        return True
    elif respuesta in ["n", "no"]:
        return False
    else:
        print("Respuesta inválida. Por defecto: No")
        return False
