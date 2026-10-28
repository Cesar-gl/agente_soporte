import json, os, time, schedule, gspread, sys
from google.genai import types
from google import genai
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request


# Limpiar variables de entorno de certificados rotos
for var in ['REQUESTS_CA_BUNDLE', 'CURL_CA_BUNDLE', 'SSL_CERT_FILE']:
    if var in os.environ:
        del os.environ[var]

load_dotenv()

ARCHIVO_CREDENCIALES = "client_secret.json"
NOMBRE_HOJA = "Respuestas de Quejas (Base de Datos)"
INTERVALO_MINUTOS = 1

# Límite de peticiones a Gemini por ejecución
MAX_ANALISIS_POR_EJECUCION = 5

cliente_gemini = None

def conectar_sheets():
    """
    Autentica el script con Google. 
    Usa el token de GitHub Actions si existe, o abre el navegador si no.
    """
    try:
        # 1. Revisar si estamos en GitHub Actions (existe la variable)
        token_json = os.getenv("GOOGLE_TOKEN_JSON")
        
        if token_json:
            print("🔑 Autenticando usando el token (GitHub Actions / Railway)...")
            # Convertir el texto JSON a diccionario
            info_token = json.loads(token_json)
            
            # Crear las credenciales usando los scopes necesarios
            creds = Credentials.from_authorized_user_info(
                info_token, 
                scopes=["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
            )
            
            # Refrescar el token para asegurar que está vigente
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            
            # Autorizar a gspread con estas credenciales
            cliente = gspread.authorize(creds)
            
        else:
            print("🌐 Autenticando mediante el navegador (uso local)...")
            # Si no hay token, usar el método normal de la Práctica 1
            cliente = gspread.oauth(credentials_filename=ARCHIVO_CREDENCIALES, authorized_user_filename="token_nuevo.json")

        # 2. Abrir la hoja de cálculo y retornar
        libro = cliente.open(NOMBRE_HOJA)
        hoja = libro.sheet1
        print(f"✅ Conectado exitosamente a la hoja: {NOMBRE_HOJA}")
        return hoja

    except FileNotFoundError:
        print(f"❌ Error: No se encontró el archivo '{ARCHIVO_CREDENCIALES}'.")
        return None
    except gspread.exceptions.SpreadsheetNotFound:
        print(f"❌ Error: No se encontró la hoja '{NOMBRE_HOJA}'.")
        return None
    except Exception as e:
        print(f"❌ Error inesperado al conectar con Google Sheets: {e}")
        return None

def analizar_con_gemini(comentario):
    """
    Envía un comentario a Gemini y devuelve un diccionario con
    la clasificación y el sentimiento detectados.
    """
    instruccion_sistema = (
        "Eres un analista de soporte al cliente. "
        "Tu única tarea es devolver una respuesta en formato JSON, "
        "sin texto adicional ni explicaciones. "
        "El JSON debe tener exactamente las claves "
        '"clasificación" y "sentimiento".'
    )

    instruccion_usuario = (
        f"Analiza el siguiente comentario de un cliente:\n\n"
        f'"{comentario}"\n\n'
        "Clasifícalo en una de estas tres categorías: "
        "Ventas, Soporte Técnico o Logística. "
        "Detecta el sentimiento: Positivo, Negativo o Neutro. "
        "Devuelve únicamente el JSON."
    )

    respuesta = cliente_gemini.models.generate_content(
        
        model = "gemini-3.8-flash", # Nota: Cambié a 1.5-flash que es la versión estable actual
        contents = instruccion_usuario,
        config = types.GenerateContentConfig(
            system_instruction=instruccion_sistema,
            # Agrega esta línea para desactivar el aviso:
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True)
        ),
    )

    texto = respuesta.text.strip()
    if texto.startswith("```"):
        texto = texto.replace("```json", "").replace("```", "").strip()

    resultado = json.loads(texto)
    return resultado

def ejecutar_agente():
    """
    Ciclo completo del agente:
    1. Conectar con Google Sheets
    2. Leer todas las filas
    3. Filtrar las que ya fueron procesadas (columna G = "SI")
    4. Analizar cada comentario nuevo con Gemini
    5. Escribir los resultados en las columnas E, F y G
    """
    print("\n" + "=" * 55)
    print("🤖 Ejecutando agente de soporte...")
    print("=" * 55)

    hoja = conectar_sheets()
    if hoja is None:
        print("⚠️ No se pudo conectar con Google Sheets. Se omite este ciclo.")
        return False

    try:
        todas_las_filas = hoja.get_all_values()
    except Exception as e:
        print(f"❌ Error al leer la hoja: {e}")
        return False

    # Omitir la primera fila (encabezados)
    filas_datos = todas_las_filas[1:]

    if not filas_datos:
        print("📭 No hay respuestas registradas todavía.")
        return True

    print(f"📋 Filas encontradas: {len(filas_datos)}")

    nuevas_procesadas = 0   
    limite_alcanzado = False

    for indice, fila in enumerate(filas_datos, start=2):
        # Protección por si la fila viene incompleta (necesitamos al menos 7 columnas A-G)
        while len(fila) < 7:
            fila.append("")

        # ── MAPEO DE COLUMNAS SEGÚN TU HOJA ──
        nombre_cliente = fila[2]     # C: Tu Nombre
        comentario     = fila[3]     # D: Describe tu queja
        ya_procesada   = fila[6]     # G: Agente Ejecutado (SI/NO)

        print(f"   🔍 DEBUG fila {indice}: col G = '{ya_procesada}' → {'SALTAR' if ya_procesada.strip().upper() == 'SI' else 'PROCESAR'}")
        
        # Filtrar: si ya tiene "SI" en la columna G, se omite
        if ya_procesada.strip().upper() == "SI":
            continue

        # Si el comentario está vacío, también se omite
        if not comentario.strip():
            print(f"⚠️ Fila {indice}: comentario vacío, se omite.")
            continue

        if nuevas_procesadas >= MAX_ANALISIS_POR_EJECUCION:
            print(f"\n⚠️ Se alcanzó el límite de {MAX_ANALISIS_POR_EJECUCION} análisis por ejecución.")
            print("   Para no agotar la cuota de la API, el resto se procesará en el próximo ciclo.")
            limite_alcanzado = True
            break

        # ── 3.2 ANALIZAR ──
        print(f"\n🔎 Fila {indice} | Cliente: {nombre_cliente or '(sin nombre)'}")
        print(f"   Comentario: {comentario[:70]}...")

        try:
            resultado = analizar_con_gemini(comentario)

        except json.JSONDecodeError as e:
            print(f"   ❌ Gemini no devolvió un JSON válido. Detalle: {e}")
            continue
        except Exception as e:
            error_str = str(e)
            # Reintento para 503 (servidor saturado)
            if "503" in error_str:
                print("   ⏳ Servidor saturado. Reintentando en 10 s...")
                time.sleep(10)
                try:
                    resultado = analizar_con_gemini(comentario)
                except Exception as e2:
                    print(f"   ❌ Falló el reintento: {e2}")
                    continue
            # Reintento para 429 (cuota excedida)
            elif "429" in error_str:
                print("   ⏳ Cuota diaria agotada. Deteniendo ejecución...")
                limite_alcanzado = True
                break
            else:
                print(f"   ❌ Error al analizar con Gemini: {e}")
                continue

        clasificacion = resultado.get("clasificación", "Desconocida")
        sentimiento   = resultado.get("sentimiento", "Desconocido")
        print(f"   ✅ Clasificación: {clasificacion}")
        print(f"   ✅ Sentimiento:   {sentimiento}")

        # ── 3.3 ESCRIBIR (Bloque corregido sin duplicados) ──
        try:
            hoja.update_cell(indice, 5, clasificacion)   # E (Columna 5)
            hoja.update_cell(indice, 6, sentimiento)     # F (Columna 6)
            hoja.update_cell(indice, 7, "SI")            # G (Columna 7)
            
            print(f"   💾 Resultados escritos en la fila {indice}.")
            nuevas_procesadas += 1
            
            # Pausa para respetar la cuota de peticiones/minuto
            time.sleep(10)
        except Exception as e:
            print(f"   ❌ Error al escribir en la hoja: {e}")
            continue

    # ── 4. RESUMEN DEL CICLO ──
    print("\n" + "-" * 55)
    if nuevas_procesadas == 0:
        print("📭 No había filas nuevas por procesar.")
    else:
        print(f"✅ Ciclo terminado. Filas procesadas: {nuevas_procesadas}")
        if limite_alcanzado:
            print(f"   (Quedaron filas pendientes para el próximo ciclo)")
    print("-" * 55)

    return True

if __name__ == "__main__":
    print("🚀 Iniciando Agente de Soporte...")

    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
    if not GEMINI_API_KEY or GEMINI_API_KEY == "TU_API_KEY_AQUI":
        print("❌ Error: Debes configurar la variable GEMINI_API_KEY con tu clave real.")
        exit(1)

    try:
        cliente_gemini = genai.Client(api_key=GEMINI_API_KEY)
        print("✅ Cliente de Gemini inicializado.")
    except Exception as e:
        print(f"❌ Error al inicializar Gemini: {e}")
        exit(1)

    # MODO "UNA VEZ" (Para GitHub Actions y pruebas)
    if "--una-vez" in sys.argv:
        print("▶️ Ejecutando en modo 'una vez'...")
        exito = ejecutar_agente()
        
        if exito:
            print("✅ Ejecución finalizada con éxito.")
            exit(0)
        else:
            print("❌ La ejecución falló.")
            exit(1)

    # MODO CONTINUO (Para Docker y Railway)
    print("▶️  Ejecutando primera revisión ahora...")
    ejecutar_agente()

    schedule.every(INTERVALO_MINUTOS).minutes.do(ejecutar_agente)
    print(f"\n⏰ Agente programado para ejecutarse cada {INTERVALO_MINUTOS} minutos.")

    while True:
        try:
            schedule.run_pending()
            time.sleep(30)
        except KeyboardInterrupt:
            print("\n🛑 Agente detenido por el usuario.")
            break
        except Exception as e:
            print(f"⚠️ Error inesperado en el bucle principal: {e}")
            time.sleep(30)