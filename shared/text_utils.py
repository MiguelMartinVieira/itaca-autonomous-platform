"""
Utilidades de limpieza y formato de texto compartidas entre workflows.
"""

import re


def limpiar_nombre_display(texto):
    """
    Extrae el nombre limpio de una persona (solo el primer nombre),
    eliminando códigos (P001, A001, MA001, etc.).
    """
    if not isinstance(texto, str):
        return ""

    # 1. Eliminar códigos tipo A###, P###, MA###, PA###
    limpio = re.sub(r"\b(?:MA|PA|[MPA])\d+\b", "", texto, flags=re.IGNORECASE)

    # 2. Eliminar coletillas de horas tipo 'x5' o 'x 5'
    limpio = re.sub(r"\bx\s*\d+\b", "", limpio, flags=re.IGNORECASE)

    # 3. Eliminar " y "
    limpio = limpio.replace(" y ", " ")

    # 4. Limpiar espacios extra
    limpio = re.sub(r"\s+", " ", limpio).strip()

    # 5. Quedarse solo con la primera palabra (nombre de pila)
    if limpio:
        limpio = limpio.split(" ")[0]

    return limpio


def limpiar_nombre_padre(texto):
    """
    Extrae el nombre limpio y natural de un padre/madre para mensajes de WhatsApp.
    Elimina códigos (MA001, PA001), palabras de parentesco (Madre, Padre, Mamá, Papá),
    paréntesis/apodos, e iniciales, preservando nombres compuestos naturales
    (ej: José María, María Luisa, María Rosa, Juan Carlos).
    """
    if not isinstance(texto, str):
        return ""

    # 1. Eliminar códigos tipo MA###, PA###, A###, P###
    limpio = re.sub(r"\b(?:MA|PA|[MPA])\d+\b", "", texto, flags=re.IGNORECASE)

    # 2. Eliminar palabras de parentesco
    limpio = re.sub(r"\b(?:Madre|Padre|Mama|Mamá|Papa|Papá)\b", "", limpio, flags=re.IGNORECASE)

    # 3. Eliminar contenido entre paréntesis o corchetes
    limpio = re.sub(r"[\(\[\{].*?[\)\]\}]", "", limpio)

    # 4. Eliminar caracteres especiales y puntuación suelta (salvo tildes y letras)
    limpio = re.sub(r"[,;:\-\_]", " ", limpio)
    limpio = re.sub(r"\s+", " ", limpio).strip()

    words = limpio.split()
    if not words:
        return ""

    # 5. Filtrar palabras de 1 o 2 letras que son iniciales/acrónimos tipo 'BC', 'FM', 'E8', 'A.'
    palabras_filtradas = []
    for w in words:
        w_clean = w.strip(".")
        # Descartar si contiene dígitos o si es acrónimo en mayúsculas de 2 letras
        if re.search(r"\d", w_clean):
            continue
        if (
            len(w_clean) <= 2
            and w_clean.isupper()
            and w_clean.lower() not in ["de", "da", "di", "al"]
        ):
            continue
        palabras_filtradas.append(w_clean)

    if not palabras_filtradas:
        return words[0].strip(".")

    primera = palabras_filtradas[0]

    # 6. Manejo de nombres compuestos habituales
    primeros_compuestos = ["maria", "maría", "jose", "josé", "juan", "ana"]
    segundos_validos = [
        "jose",
        "josé",
        "maria",
        "maría",
        "carlos",
        "luis",
        "luisa",
        "belen",
        "belén",
        "pilar",
        "carmen",
        "teresa",
        "jesus",
        "jesús",
        "manuel",
        "ignacio",
        "elena",
        "rosa",
        "paz",
    ]

    if len(palabras_filtradas) > 1 and primera.lower() in primeros_compuestos:
        segunda = palabras_filtradas[1]
        if segunda.lower() in segundos_validos:
            return f"{primera} {segunda}"

    return primera


def normalizar_identidad(texto):
    """
    Normaliza un nombre o texto para comparaciones robustas:
    minúsculas, sin acentos, sin códigos y sin espacios extra.
    """
    if not texto:
        return ""
    # Quitar códigos antes de normalizar
    texto = re.sub(r"\b(?:MA|PA|[MPA])\d+\b", "", str(texto), flags=re.IGNORECASE)
    texto = texto.lower().strip()
    reemplazos = {"á": "a", "é": "e", "í": "i", "ó": "o", "ú": "u", "ñ": "n"}
    for k, v in reemplazos.items():
        texto = texto.replace(k, v)
    return re.sub(r"\s+", " ", texto).strip()


def extraer_horas(texto):
    """Extrae y suma las horas de un texto (ej: 'A001 x5 A002 x3' → 8)."""
    if not isinstance(texto, str):
        return 0
    matches = re.findall(r"x\s*(\d+)", texto, flags=re.IGNORECASE)
    if not matches:
        return 0
    return sum(int(m) for m in matches)


def formatear_deuda(deuda):
    """Formatea una deuda como string (sin decimales si es entero)."""
    if isinstance(deuda, (int, float)):
        return f"{int(deuda)}" if float(deuda).is_integer() else f"{deuda:.2f}"
    return str(deuda)


def formatear_periodo(texto):
    """Convierte texto de periodo a mensaje amigable en español."""
    if not isinstance(texto, str):
        return "durante este periodo"
    t = texto.lower().strip()
    if "semanal" in t:
        return "durante esta semana"
    elif "2 semanas" in t:
        return "durante estas dos semanas"
    elif "final mes" in t or "mes" in t:
        return "durante este mes"
    return f"durante {texto}"


def formatear_nombre_alumno(texto):
    """Convierte 'hijo'/'hija' a 'tu hijo'/'tu hija', o devuelve el nombre original."""
    if not isinstance(texto, str):
        return ""
    t_lower = texto.lower().strip()
    if t_lower == "hijo":
        return "tu hijo"
    if t_lower == "hija":
        return "tu hija"
    return texto


# --- Lógica avanzada de Formularios (movida de workflows/formularios.py) ---


def limpiar_alumno_texto(alumno_raw):
    """Elimina códigos tipo A078 del nombre del alumno y pasa a minúscula palabras comunes."""
    if not isinstance(alumno_raw, str):
        return ""
    limpio = re.sub(r"\bA\d+\b", "", alumno_raw, flags=re.IGNORECASE)

    palabras_comunes = [
        "hijo",
        "hija",
        "hijos",
        "hijas",
        "amigo",
        "amiga",
        "amigos",
        "amigas",
        "chico",
        "chica",
        "hermano",
        "hermana",
        "hermanos",
        "hermanas",
    ]
    for palabra in palabras_comunes:
        limpio = re.sub(rf"\b{palabra}\b", palabra.lower(), limpio, flags=re.IGNORECASE)

    limpio = re.sub(r"\s+", " ", limpio)
    limpio = limpio.replace(" , ", ", ").strip()
    return limpio


def formatear_alumno_detalle(alumno_raw):
    """
    Formatea el nombre del alumno detectando patrones de Hijo/Hija.
    Ej: "Hijo 1 Marta Mendoza" → "al hijo de Marta Mendoza"
    """
    if not isinstance(alumno_raw, str):
        return ""

    alumno_limpio = limpiar_alumno_texto(alumno_raw)

    # Detectar múltiples Hijo/Hija del mismo padre
    # Buscamos patrones tipo "Hijo 1, Hijo 2 de Marta"
    # El regex busca (Hijo/a) + opcional número + opcional 'de' + nombre
    patron_multi = r"(?:hijo|hija)(?:\s+\d+)?(?:\s+y\s+|,?\s+)?(?:hijo|hija)(?:\s+\d+)?\s+de\s+(.+)"
    m_multi = re.search(patron_multi, alumno_limpio, re.IGNORECASE)
    if m_multi:
        padre = m_multi.group(1).strip()
        # Determinar género predominante (simplificado: primer hijo)
        genero = "hijo" if "hijo" in alumno_limpio.lower()[:10] else "hija"
        if genero == "hijo":
            return f"a los hijos de {padre}"
        else:
            return f"a las hijas de {padre}"

    # Patrón de un solo hijo: "Hijo de Marta" o "Hijo Marta"
    match = re.match(r"^(hijo|hija)(?:\s+\d+)?\s+(?:de\s+)?(.+)$", alumno_limpio, re.IGNORECASE)
    if match:
        genero = match.group(1).lower()
        nombre_padre = match.group(2).strip()
        if genero == "hijo":
            return f"al hijo de {nombre_padre}"
        elif genero == "hija":
            return f"a la hija de {nombre_padre}"

    return f"a {alumno_limpio}" if alumno_limpio else ""


def construir_detalles_clases(lista_clases, MESES_ES, formatear_fecha_fn):
    """
    Construye el texto de clases para el mensaje WhatsApp con formato de lista 📌.
    Agrupa fechas del mismo alumno y consolida múltiples Hijo/Hija del mismo padre.
    """
    from collections import OrderedDict

    def _formatear_fechas_grupo(fechas):
        if len(fechas) == 1:
            return formatear_fecha_fn(fechas[0])

        parsed = []
        for f in fechas:
            try:
                anio, mes, dia = f.split("-")
                parsed.append((int(anio), int(mes), int(dia)))
            except Exception:
                parsed.append(None)

        parsed_ok = [p for p in parsed if p is not None]
        meses_unicos = set((p[0], p[1]) for p in parsed_ok)

        if len(meses_unicos) == 1:
            mes_int = parsed_ok[0][1]
            nombre_mes = MESES_ES.get(mes_int, "")
            dias = sorted(set(p[2] for p in parsed_ok))
            if len(dias) == 2:
                dias_str = f"{dias[0]} y {dias[1]}"
            else:
                dias_str = ", ".join(str(d) for d in dias[:-1]) + f" y {dias[-1]}"
            return f"{dias_str} de {nombre_mes}"
        else:
            fechas_ordenadas = sorted(set(fechas))
            fechas_fmt = [formatear_fecha_fn(f) for f in fechas_ordenadas]
            if len(fechas_fmt) == 2:
                return f"{fechas_fmt[0]} y {fechas_fmt[1]}"
            return ", ".join(fechas_fmt[:-1]) + " y " + fechas_fmt[-1]

    grupos = OrderedDict()
    for clase in lista_clases:
        key = clase["alumno"].strip()
        if key not in grupos:
            grupos[key] = []
        grupos[key].append(clase["fecha"])

    hijos_por_padre = {}
    for alumno_raw in grupos:
        alumno_limpio = limpiar_alumno_texto(alumno_raw)
        m = re.match(r"^(Hijo|Hija)(?:\s+\d+)?\s+(.+)$", alumno_limpio, re.IGNORECASE)
        if m:
            genero = m.group(1).lower()
            padre = m.group(2).strip()
            if padre not in hijos_por_padre:
                hijos_por_padre[padre] = []
            hijos_por_padre[padre].append((alumno_raw, genero))

    padres_consolidados = {padre for padre, hijos in hijos_por_padre.items() if len(hijos) > 1}

    lineas = []
    padres_procesados = set()

    for alumno_raw, fechas in grupos.items():
        alumno_limpio = limpiar_alumno_texto(alumno_raw)
        m = re.match(r"^(Hijo|Hija)(?:\s+\d+)?\s+(.+)$", alumno_limpio, re.IGNORECASE)

        if m:
            padre = m.group(2).strip()
            if padre in padres_consolidados:
                if padre not in padres_procesados:
                    padres_procesados.add(padre)
                    todas_fechas = []
                    for a_raw, _ in hijos_por_padre[padre]:
                        todas_fechas.extend(grupos[a_raw])
                    todas_fechas = sorted(set(todas_fechas))
                    fechas_fmt = _formatear_fechas_grupo(todas_fechas)
                    lineas.append(f"📌 {fechas_fmt} a los hijos de {padre}")
                continue

        fechas_fmt = _formatear_fechas_grupo(fechas)
        alumno_formateado = formatear_alumno_detalle(alumno_raw)
        lineas.append(f"📌 {fechas_fmt} {alumno_formateado}")

    return "\n".join(lineas), len(lista_clases)
