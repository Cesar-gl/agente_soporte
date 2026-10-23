import json
import os
import sys
from dotenv import load_dotenv
import time
import schedule
import gspread
from google import genai
from google.genai import types
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

# ═════════════════════════════════════════════════════════════════════
# ⚙️ CONFIGURACIÓN DEL AGENTE Y GOOGLE FORMS / SHEETS
# ═════════════════════════════════════════════════════════════════════
# Limpiar las variables de entorno de certificados que apuntan a rutas rotas o del gym
for var in ['REQUESTS_CA_BUNDLE', 'CURL_CA_BUNDLE', 'SSL_CERT_FILE']:
    if var in os.environ:
        del os.environ[var]

# 1. Credenciales y Acceso
ARCHIVO_CREDENCIALES = "client_secrets.json"
load_dotenv()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "TU_API_KEY_AQUI")

# 2. Hoja de Cálculo vinculada a tu Google Form
NOMBRE_O_ID_HOJA = "Respuestas de Quejas (Base de Datos)"

# Nombre de la pestaña (deja None para usar la primera pestaña por defecto)
NOMBRE_PESTANA = None 

# 3. Mapeo de Columnas (1 = A, 2 = B, 3 = C, 4 = D, 5 = E, 6 = F, etc.)
COL_NOMBRE         = 2   # Columna B: Nombre del cliente (o contacto)
COL_COMENTARIO     = 3   # Columna C: Comentario / Queja / Respuesta del cliente
COL_CLASIFICACION  = 4   # Columna D: Clasificación IA (Ventas, Soporte, etc.)
COL_SENTIMIENTO    = 5   # Columna E: Sentimiento IA (Positivo, Negativo, Neutro)
COL_EJECUTADO      = 6   # Columna F: Marca de procesamiento ("SI")

# 4. Parámetros de la IA y Ejecución
MODELO_GEMINI = "gemini-2.5-flash"
INTERVALO_MINUTOS = 1
PAUSA_ENTRE_LLAMADAS_SEG = 30

# Categorías personalizables para tu negocio
CATEGORIAS = "Ventas, Soporte Técnico, Logística"
SENTIMIENTOS = "Positivo, Negativo, Neutro"

# ═════════════════════════════════════════════════════════════════════

# Declaramos la variable global aquí
cliente_gemini = None


def conectar_sheets():
    try:
        google_token_env = os.getenv("GOOGLE_TOKEN_JSON")
        if google_token_env:
            try:
                token_info = json.loads(google_token_env)
                creds = Credentials.from_authorized_user_info(
                    token_info, 
                    scopes=['https://www.googleapis.com/auth/spreadsheets']
                )
                if creds and creds.expired and creds.refresh_token:
                    creds.refresh(Request())
                
                cliente_gs = gspread.authorize(creds)
                print("✅ Conectado a Google Sheets usando el token de la nube (GitHub Actions).")
            except Exception as e:
                print(f"❌ Error al autenticar con GOOGLE_TOKEN_JSON: {e}")
                return None
        else:
            if not os.path.exists(ARCHIVO_CREDENCIALES):
                print(f"❌ Error: No se encontró el archivo local '{ARCHIVO_CREDENCIALES}'.")
                return None

            cliente_gs = gspread.oauth(
                credentials_filename=ARCHIVO_CREDENCIALES,
                authorized_user_filename="token.json"
            )
            print("✅ Conectado a Google Sheets usando credenciales locales.")

        if len(NOMBRE_O_ID_HOJA) > 30 and "/" not in NOMBRE_O_ID_HOJA and " " not in NOMBRE_O_ID_HOJA:
            libro = cliente_gs.open_by_key(NOMBRE_O_ID_HOJA)
        else:
            libro = cliente_gs.open(NOMBRE_O_ID_HOJA)

        if NOMBRE_PESTANA:
            hoja = libro.worksheet(NOMBRE_PESTANA)
        else:
            hoja = libro.sheet1

        print(f"✅ Hoja abierta exitosamente: '{libro.title}' (Pestaña: '{hoja.title}')")
        return hoja

    except gspread.exceptions.SpreadsheetNotFound:
        print(f"❌ Error: No se encontró la hoja '{NOMBRE_O_ID_HOJA}'.")
        return None
    except Exception as e:
        print(f"❌ Error inesperado al conectar con Google Sheets: {e}")
        return None


def analizar_con_gemini(comentario):
    # Le indicamos a Python que usaremos la variable global
    global cliente_gemini

    instruccion_sistema = (
        "Eres un analista de soporte y servicio al cliente de alta precisión. "
        "Tu única tarea es devolver una respuesta en formato JSON estricto, "
        "sin texto introductorio, sin explicaciones y sin bloques markdown. "
        'El JSON debe tener exactamente las claves "clasificación" y "sentimiento".'
    )

    instruccion_usuario = (
        f"Analiza el siguiente comentario recibido en el formulario:\n\n"
        f'"{comentario}"\n\n'
        f"Clasifícalo en una de estas categorías: {CATEGORIAS}.\n"
        f"Detecta el sentimiento: {SENTIMIENTOS}.\n"
        "Devuelve únicamente el objeto JSON."
    )

    if cliente_gemini is None:
        raise ValueError("El cliente de Gemini no ha sido inicializado.")

    respuesta = cliente_gemini.models.generate_content(
        model=MODELO_GEMINI,
        contents=instruccion_usuario,
        config=types.GenerateContentConfig(
            system_instruction=instruccion_sistema,
            response_mime_type="application/json",
            temperature=0.2
        ),
    )

    texto = respuesta.text.strip()
    if texto.startswith("```"):
        texto = texto.replace("```json", "").replace("```", "").strip()

    return json.loads(texto)


def ejecutar_agente():
    print("\n" + "=" * 60)
    print("🤖 Iniciando ciclo de revisión de respuestas...")
    print("=" * 60)

    hoja = conectar_sheets()
    if hoja is None:
        print("⚠️ No se pudo acceder a Google Sheets.")
        return False

    try:
        todas_las_filas = hoja.get_all_values()
    except Exception as e:
        print(f"❌ Error al leer las filas: {e}")
        return False

    filas_datos = todas_las_filas[1:]
    if not filas_datos:
        print("📭 Aún no hay respuestas registradas en el formulario.")
        return True

    print(f"📋 Total de respuestas encontradas: {len(filas_datos)}")

    nuevas_procesadas = 0
    max_col_necesaria = max(COL_NOMBRE, COL_COMENTARIO, COL_CLASIFICACION, COL_SENTIMIENTO, COL_EJECUTADO)

    for indice, fila in enumerate(filas_datos, start=2):
        while len(fila) < max_col_necesaria:
            fila.append("")

        nombre_cliente = fila[COL_NOMBRE - 1]
        comentario     = fila[COL_COMENTARIO - 1]
        ya_procesada   = fila[COL_EJECUTADO - 1]

        if ya_procesada.strip().upper() == "SI":
            continue

        if not comentario.strip():
            print(f"⚠️ Fila {indice}: Comentario vacío, omitiendo.")
            continue

        print(f"\n🔎 Procesando Fila {indice} | Cliente: {nombre_cliente or '(Anónimo / No especificado)'}")
        print(f"   💬 Comentario: {comentario[:80]}...")

        resultado = None
        for intento in range(3):
            try:
                resultado = analizar_con_gemini(comentario)
                break
            except Exception as e:
                error_str = str(e)
                if "503" in error_str or "overloaded" in error_str.lower():
                    print(f"   ⏳ Servidor saturado (intento {intento + 1}/3). Esperando 10s...")
                    time.sleep(10)
                elif "429" in error_str or "quota" in error_str.lower():
                    print(f"   ⏳ Límite de cuota alcanzado (intento {intento + 1}/3). Esperando 20s...")
                    time.sleep(20)
                else:
                    print(f"   ❌ Error al analizar con Gemini: {e}")
                    break

        if not resultado:
            print(f"   ⚠️ No se pudo procesar la fila {indice}. Se intentará en la siguiente vuelta.")
            continue

        clasificacion = resultado.get("clasificación", "Desconocida")
        sentimiento   = resultado.get("sentimiento", "Desconocido")
        print(f"   🏷️ Clasificación: {clasificacion}")
        print(f"   ❤️ Sentimiento:   {sentimiento}")

        try:
            hoja.update_cell(indice, COL_CLASIFICACION, clasificacion)
            hoja.update_cell(indice, COL_SENTIMIENTO, sentimiento)
            hoja.update_cell(indice, COL_EJECUTADO, "SI")
            print(f"   💾 Fila {indice} actualizada exitosamente.")
            nuevas_procesadas += 1
        except Exception as e:
            print(f"   ❌ Error al escribir en Google Sheets: {e}")
            continue

        time.sleep(PAUSA_ENTRE_LLAMADAS_SEG)

    print("\n" + "-" * 60)
    if nuevas_procesadas == 0:
        print("📭 No había comentarios nuevos por procesar.")
    else:
        print(f"✅ Ciclo finalizado. Total de respuestas procesadas: {nuevas_procesadas}")
    print("-" * 60)
    return True


if __name__ == "__main__":
    print("🚀 Iniciando Agente de Análisis de Google Forms...")

    if not GEMINI_API_KEY or GEMINI_API_KEY == "TU_API_KEY_AQUI":
        print("❌ Error: Debes configurar GEMINI_API_KEY en tu archivo .env.")
        sys.exit(1)

    if not os.getenv("GOOGLE_TOKEN_JSON"):
        if not os.path.exists(ARCHIVO_CREDENCIALES):
            print(f"❌ Error: Falta el archivo '{ARCHIVO_CREDENCIALES}' para ejecución local.")
            sys.exit(1)

    # Inicializar cliente Gemini (Sin el 'global' problemático)
    def analizar_con_gemini(comentario):
    global cliente_gemini  # <--- ESTA LÍNEA ES VITAL AQUÍ
    """
    Envía el comentario del formulario a Gemini y retorna
    """
    # ... el resto del código

    if "--una-vez" in sys.argv:
        print("⚡ Ejecutando en modo único (--una-vez para GitHub Actions)...")
        exito = ejecutar_agente()
        if exito:
            sys.exit(0)
        else:
            sys.exit(1)
    else:
        print("▶️ Ejecutando primera revisión local...")
        ejecutar_agente()

        schedule.every(INTERVALO_MINUTOS).minutes.do(ejecutar_agente)
        print(f"\n⏰ Agente programado localmente para revisar cada {INTERVALO_MINUTOS} minuto(s).")
        print("   Presiona Ctrl+C en cualquier momento para detenerlo.\n")

        while True:
            try:
                schedule.run_pending()
                time.sleep(15)
            except KeyboardInterrupt:
                print("\n🛑 Agente detenido por el usuario. ¡Hasta pronto!")
                break
            except Exception as e:
                print(f"⚠️ Error en el bucle principal: {e}")
                time.sleep(15)