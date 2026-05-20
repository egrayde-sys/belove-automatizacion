import asyncio
import os
import json
import random
import time
import requests
import pandas as pd
from playwright.async_api import async_playwright
from google.oauth2.service_account import Credentials
import gspread
import base64

# ── CONFIGURACIÓN ─────────────────────────────────────────────
SLACK_TOKEN    = os.environ.get("SLACK_TOKEN")
SLACK_CHANNEL  = os.environ.get("SLACK_CHANNEL", "#belove")
EMAIL          = os.environ.get("EROSHOP_EMAIL")
PASSWORD       = os.environ.get("EROSHOP_PASSWORD")
BASE_URL       = "https://www.eroshopmayorista.cl"
DELAY          = 2000
UMBRAL_CALIDAD = 0.10
GITHUB_TOKEN   = os.environ.get("GITHUB_TOKEN")
GIST_ID        = "c6a5fca73c46f6a98bef5f47dc8ff123"
SHEET_NAME     = "Belove - Automatización"
URL_BELOVE     = "https://belove.cl/ws/json_productos.php?token=WGRjRWs1WHU1dWdRZ1VCeHV0YVo="

# ── SLACK ─────────────────────────────────────────────────────
def enviar_slack(mensaje):
    try:
        resp = requests.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {SLACK_TOKEN}"},
            json={"channel": SLACK_CHANNEL, "text": mensaje}
        )
        data = resp.json()
        if data.get("ok"):
            print("✅ Slack enviado")
        else:
            print(f"⚠️ Slack error: {data.get('error')}")
    except Exception as e:
        print(f"⚠️ Slack excepción: {e}")

# ── GOOGLE SHEETS ─────────────────────────────────────────────
def conectar_sheets():
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive"
    ]
    creds_raw = os.environ.get("GOOGLE_CREDENTIALS")
    print(f"DEBUG GOOGLE_CREDENTIALS: {'OK longitud='+str(len(creds_raw)) if creds_raw else 'NONE'}")
    creds_json = json.loads(base64.b64decode(creds_raw).decode())
    creds = Credentials.from_service_account_info(creds_json, scopes=scope)
    client = gspread.authorize(creds)
    print(f"DEBUG SHEET_NAME: '{SHEET_NAME}'")
    sheets_disponibles = [s.title for s in client.openall()]
    print(f"DEBUG Sheets disponibles: {sheets_disponibles}")
    return client.open(SHEET_NAME)

# ── SCRAPING ──────────────────────────────────────────────────
async def crear_sesion(playwright):
    browser = await playwright.chromium.launch(headless=True)
    page = await browser.new_page()
    await page.goto(f"{BASE_URL}/customer/login")
    await page.fill("input[name='customer[email]']", EMAIL)
    await page.fill("input[name='customer[password]']", PASSWORD)
    await page.click("input[name='commit']")
    await page.wait_for_timeout(3000)
    if "login" in page.url:
        raise Exception("Login fallido")
    print(f"✅ Login exitoso — {page.url}")
    return browser, page

async def extraer_producto(page, url):
    await page.goto(url, timeout=60000)
    await page.wait_for_timeout(DELAY)

    nombre = ""
    el = await page.query_selector("h1")
    if el:
        nombre = (await el.inner_text()).strip()

    img = ""
    for selector in [".product-image img", "figure img", ".main-image img"]:
        el = await page.query_selector(selector)
        if el:
            img = await el.get_attribute("src") or ""
            if img:
                break

    # Verificar si tiene variantes
    selects = await page.query_selector_all("select.prod-options-selects")

    if not selects:
        # Producto simple sin variantes
        sku = ""
        el = await page.query_selector(".sku_elem")
        if el:
            sku = (await el.inner_text()).strip()

        stock = ""
        el = await page.query_selector("#stock")
        if el:
            texto = (await el.inner_text()).strip()
            stock = texto.replace("DISPONIBILIDAD:", "").replace("Disponibilidad:", "").strip()
            if stock == "":
                stock = "0"

        precio = ""
        for selector in [".product-form_price", ".product-price", ".price", "[class*='price']", ".unit-price"]:
            el = await page.query_selector(selector)
            if el:
                texto = (await el.inner_text()).strip()
                if texto:
                    precio = texto.replace("$", "").replace(".", "").replace(" + IVA", "").replace("+IVA", "").strip()
                    try:
                        precio = str(int(precio))
                    except:
                        precio = texto
                    break

        return [{"nombre": nombre, "sku": sku, "stock": stock, "precio_neto": precio, "url": url, "imagen": img}]

    else:
        # Producto con variantes — iterar cada opción del primer select
        variantes = []
        opciones = await selects[0].query_selector_all("option")

        for opcion in opciones:
            value = await opcion.get_attribute("value")
            if not value:
                continue

            # Seleccionar la opción
            await selects[0].select_option(value=value)
            await page.wait_for_timeout(1000)

            # Capturar SKU
            sku = ""
            el = await page.query_selector(".sku_elem")
            if el:
                sku = (await el.inner_text()).strip()

            # Capturar stock desde data-variant-stock
            stock_attr = await opcion.get_attribute("data-variant-stock")
            stock = stock_attr if stock_attr else "0"

            # Capturar precio
            precio = ""
            for selector in [".product-form_price", ".product-price", ".price", "[class*='price']", ".unit-price"]:
                el = await page.query_selector(selector)
                if el:
                    texto = (await el.inner_text()).strip()
                    if texto:
                        precio = texto.replace("$", "").replace(".", "").replace(" + IVA", "").replace("+IVA", "").strip()
                        try:
                            precio = str(int(precio))
                        except:
                            precio = texto
                        break

            if sku:
                variantes.append({
                    "nombre": nombre,
                    "sku": sku,
                    "stock": stock,
                    "precio_neto": precio,
                    "url": url,
                    "imagen": img
                })

        return variantes if variantes else [{"nombre": nombre, "sku": "", "stock": "0", "precio_neto": "", "url": url, "imagen": img}]

async def scraping_eroshop():
    async with async_playwright() as pw:
        browser, page = await crear_sesion(pw)

        print("📄 Recorriendo catálogo y fabricante...")
        product_urls = []

        # Scraping 1: /catalogo (15 páginas)
        for pg in range(1, 16):
            url = f"{BASE_URL}/catalogo" if pg == 1 else f"{BASE_URL}/catalogo?page={pg}"
            await page.goto(url)
            await page.wait_for_timeout(DELAY)
            links = await page.query_selector_all("h3 a")
            for link in links:
                href = await link.get_attribute("href")
                if href:
                    if href.startswith("/"):
                        href = BASE_URL + href
                    if href not in product_urls:
                        product_urls.append(href)
            print(f"  Catálogo página {pg}/15 → acumulado: {len(product_urls)}")

        print(f"✅ Catálogo: {len(product_urls)} URLs")

        # Scraping 2: /fabricante (13 páginas)
        urls_antes = len(product_urls)
        for pg in range(1, 14):
            url = f"{BASE_URL}/fabricante" if pg == 1 else f"{BASE_URL}/fabricante?page={pg}"
            await page.goto(url)
            await page.wait_for_timeout(DELAY)
            links = await page.query_selector_all(".product-block a")
            for link in links:
                href = await link.get_attribute("href")
                if href and not href.endswith((".jpg", ".png", ".gif")):
                    if href.startswith("/"):
                        href = BASE_URL + href
                    if href not in product_urls:
                        product_urls.append(href)
            print(f"  Fabricante página {pg}/13 → acumulado: {len(product_urls)}")

        print(f"✅ Fabricante agregó: {len(product_urls) - urls_antes} URLs nuevas")
        print(f"\n🔍 Extrayendo {len(product_urls)} productos...")

        productos = []
        for i, url in enumerate(product_urls, 1):
            intentos = 0
            while intentos < 3:
                try:
                    datos = await extraer_producto(page, url)
                    productos.extend(datos)
                    if i % 50 == 0:
                        print(f"  [{i}/{len(product_urls)}] {dato['nombre'][:35]}")
                    break
                except Exception as e:
                    if "closed" in str(e).lower() or "target" in str(e).lower():
                        intentos += 1
                        print(f"  ⚠️ Reconectando... (intento {intentos}/3)")
                        try:
                            await browser.close()
                        except:
                            pass
                        browser, page = await crear_sesion(pw)
                    else:
                        print(f"  ⚠️ Error en {url}: {e}")
                        break

        await browser.close()

        df = pd.DataFrame(productos)
        df["stock"] = pd.to_numeric(df["stock"], errors="coerce").fillna(0).astype(int)
        df["precio_neto"] = pd.to_numeric(df["precio_neto"], errors="coerce").fillna(0).astype(int)
        df = df.replace([float('inf'), float('-inf')], 0)
        df = df.fillna("")
        df = df[df["sku"] != ""].drop_duplicates(subset=["sku"], keep="first")

        print(f"DEBUG df scraping sample: {df[['sku','stock','precio_neto']].head(3).to_string()}")

        total = len(df)
        sin_sku = df[df["sku"] == ""].shape[0]
        sin_precio = df[df["precio_neto"] == 0].shape[0]
        alertas = []
        if total > 0:
            if sin_sku / total > UMBRAL_CALIDAD:
                alertas.append(f"🚨 {sin_sku} productos sin SKU ({sin_sku/total:.0%})")
            if sin_precio / total > UMBRAL_CALIDAD:
                alertas.append(f"🚨 {sin_precio} productos sin precio ({sin_precio/total:.0%})")

        print(f"\n✅ Scraping completo: {total} productos únicos")
        return df, alertas

# ── CRUCE Y EXPORTAR ──────────────────────────────────────────
def procesar_cruce(df_eroshop, sheet):
    df_belove = pd.DataFrame(requests.get(URL_BELOVE).json())
    df_belove["sku"] = df_belove["sku"].astype(str).str.strip()
    df_eroshop["sku"] = df_eroshop["sku"].astype(str).str.strip()

    ws_costos = sheet.worksheet("costos_especiales")
    df_costos = pd.DataFrame(ws_costos.get_all_records())
    df_costos["sku"] = df_costos["sku"].astype(str).str.strip()

    skus_eroshop = set(df_eroshop["sku"])
    df_costos_nuevos = df_costos[~df_costos["sku"].isin(skus_eroshop)].copy()
    if len(df_costos_nuevos) > 0:
        df_china = df_costos_nuevos.merge(df_belove[["sku", "nombre", "stock"]], on="sku", how="left")
        df_china["precio_neto"] = 0
        df_china["origen"] = "china"
        df_china = df_china[["nombre", "sku", "stock", "precio_neto", "origen"]]
        df_eroshop["origen"] = "eroshop"
        df_eroshop = pd.concat([df_eroshop, df_china], ignore_index=True)

    df_cruce = df_eroshop.merge(
        df_belove[["sku", "id", "precio", "precio_descuento", "stock"]],
        on="sku", how="left", suffixes=("_eroshop", "_belove")
    ).rename(columns={
        "precio_neto": "costo_neto",
        "stock_eroshop": "stock_eroshop",
        "precio": "precio_actual_belove",
        "precio_descuento": "precio_descuento_belove",
        "stock_belove": "stock_belove",
    })

    # Subir eroshop_raw
    ws_raw = sheet.worksheet("eroshop_raw")
    ws_raw.clear()
    df_eroshop_upload = df_eroshop[["nombre", "sku", "stock", "precio_neto"]].copy()
    df_eroshop_upload = df_eroshop_upload.replace([float('inf'), float('-inf')], 0)
    df_eroshop_upload = df_eroshop_upload.fillna("")
    df_eroshop_upload = df_eroshop_upload.astype(str)
    print("DEBUG subiendo eroshop_raw...")
    ws_raw.update(range_name="A1", values=[["nombre", "sku", "stock", "precio_neto"]] + df_eroshop_upload.values.tolist())
    print("DEBUG eroshop_raw OK")

    # Subir belove_raw
    df_belove_raw = df_belove[["sku", "precio", "precio_descuento", "stock"]].fillna("")
    ws_belove = sheet.worksheet("belove_raw")
    ws_belove.clear()
    ws_belove.update(range_name="A1", values=[df_belove_raw.columns.tolist()] + df_belove_raw.values.tolist())

    # Subir cruce con fórmulas
    encabezados = [
        "sku", "nombre", "costo_neto", "costo_bruto", "precio_calculado",
        "precio_actual_belove", "precio_descuento_belove", "stock_eroshop", "stock_belove",
        "cambio_precio", "cambio_stock", "producto_nuevo",
        "ganancia_bruta", "margen_bruto", "ganancia_real", "margen_real",
        "precio_descuento", "precio"
    ]
    filas = []
    for i, (_, row) in enumerate(df_cruce.iterrows(), start=2):
        fila = [
            row.get("sku", ""), row.get("nombre", ""), row.get("costo_neto", ""),
            f"=IF(ISERROR(VLOOKUP(A{i};costos_especiales!$A:$B;2;0));ROUND(C{i}*config!$B$2;0);VLOOKUP(A{i};costos_especiales!$A:$B;2;0))",
            f"=FLOOR(D{i}*config!$B$3;1000)+990",
            row.get("precio_actual_belove", ""), row.get("precio_descuento_belove", ""),
            row.get("stock_eroshop", ""), row.get("stock_belove", ""),
            f'=IF(E{i}<>G{i};"SÍ";"NO")',
            f'=IF(H{i}<>I{i};"SÍ";"NO")',
            f'=IF(ISERROR(VLOOKUP(A{i};belove_raw!A:A;1;0));"NUEVO";"EXISTE")',
            f"=E{i}-D{i}",
            f"=ROUND(M{i}/E{i}*100;1)",
            f"=E{i}-D{i}-VLOOKUP(\"bolsa\";config!A:B;2;0)-ROUND(E{i}*VLOOKUP(\"comision\";config!A:B;2;0);0)-IF(E{i}>=VLOOKUP(\"despacho_gratis_desde\";config!A:B;2;0);VLOOKUP(\"costo_despacho\";config!A:B;2;0);0)",
            f"=ROUND(O{i}/E{i}*100;1)",
            f"=E{i}",
            "",
        ]
        filas.append(fila)

    filas_limpias = []
    for fila in filas:
        fila_limpia = []
        for v in fila:
            if isinstance(v, float) and (v != v or v == float('inf') or v == float('-inf')):
                fila_limpia.append(0)
            elif v is None:
                fila_limpia.append("")
            else:
                fila_limpia.append(v)
        filas_limpias.append(fila_limpia)

    print("DEBUG filas limpias OK")
    ws_cruce = sheet.worksheet("cruce")
    ws_cruce.clear()
    ws_cruce.update(range_name="A1", values=[encabezados] + filas_limpias, value_input_option="USER_ENTERED")
    print("DEBUG cruce subido OK")

    # Leer cruce procesado
    data_cruce = ws_cruce.get_all_records(value_render_option='UNFORMATTED_VALUE', expected_headers=[])
    df_resultado = pd.DataFrame(data_cruce)
    print(f"DEBUG df_resultado shape: {df_resultado.shape}")
    df_resultado = df_resultado.replace(['#DIV/0!', '#ERROR!', '#N/A', '#VALUE!', '#REF!', '#NAME?'], 0)
    df_resultado = df_resultado.replace([float('inf'), float('-inf')], 0)
    df_resultado = df_resultado.fillna(0)
    for col in df_resultado.columns:
        try:
            df_resultado[col] = pd.to_numeric(df_resultado[col])
        except:
            pass
    df_resultado = df_resultado.replace([float('inf'), float('-inf')], 0)
    df_resultado = df_resultado.fillna(0)
    print("DEBUG limpieza OK")

    # Exportar TODOS los productos
    df_todos = df_resultado.copy().reset_index(drop=True)

    # Buscar ID en Belove normalizando SKU con/sin guión
    def buscar_id_belove(sku):
        sku = str(sku).strip()
        match = df_belove[df_belove["sku"] == sku]
        if len(match) > 0:
            return int(match.iloc[0]["id"])
        sku_norm = sku.replace("-", "").upper()
        match = df_belove[df_belove["sku"].str.replace("-", "").str.upper() == sku_norm]
        if len(match) > 0:
            return int(match.iloc[0]["id"])
        return 0

    df_todos["id"] = df_todos["sku"].apply(buscar_id_belove)

    # Calcular precios
    def calcular_precio(row):
        precio_descuento = int(row["precio_descuento"]) if row["precio_descuento"] else 0
        if row["producto_nuevo"] == "NUEVO":
            pct = random.uniform(0.15, 0.69)
            return int((precio_descuento * (1 + pct) // 1000) * 1000 + 990)
        return int(row["precio_actual_belove"]) if row["precio_actual_belove"] else precio_descuento

    df_todos["precio_final"] = df_todos.apply(calcular_precio, axis=1)
    df_todos["precio_descuento_final"] = df_todos["precio_descuento"].apply(lambda x: int(x) if x else 0)
    df_todos["stock_final"] = df_todos["stock_eroshop"].apply(lambda x: int(x) if x else 0)

    df_exportar = df_todos[["id", "sku", "precio_final", "precio_descuento_final", "stock_final"]].copy()
    df_exportar.columns = ["id", "sku", "precio", "precio_descuento", "stock"]
    df_exportar = df_exportar.fillna(0)

    resumen = {
        "total_productos": len(df_resultado),
        "cambio_precio": int((df_resultado["cambio_precio"] == "SÍ").sum()),
        "cambio_stock": int((df_resultado["cambio_stock"] == "SÍ").sum()),
        "productos_nuevos": int((df_resultado["producto_nuevo"] == "NUEVO").sum()),
        "a_actualizar": len(df_exportar),
    }

    print(f"DEBUG df_exportar sample:\n{df_exportar.head(3).to_string()}")
    return df_exportar, resumen

# ── ACTUALIZAR GIST ───────────────────────────────────────────
def actualizar_gist(df_exportar):
    df_exportar = df_exportar.copy()
    df_exportar = df_exportar.replace([float('inf'), float('-inf')], 0)
    df_exportar = df_exportar.fillna(0)
    exportar_json = df_exportar.to_dict(orient="records")
    for item in exportar_json:
        for k, v in item.items():
            if isinstance(v, float) and (v != v or v == float('inf') or v == float('-inf')):
                item[k] = 0

    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json"
    }
    payload = {
        "files": {
            "exportar.json": {
                "content": json.dumps(exportar_json, ensure_ascii=False, indent=2)
            }
        }
    }
    resp = requests.patch(f"https://api.github.com/gists/{GIST_ID}", headers=headers, json=payload)
    if resp.status_code == 200:
        url = resp.json()["files"]["exportar.json"]["raw_url"]
        print(f"✅ Gist actualizado: {url}")
        return url
    else:
        raise Exception(f"Error Gist: {resp.status_code} — {resp.text[:200]}")

# ── MAIN ──────────────────────────────────────────────────────
async def main():
    print("🚀 Iniciando automatización Belove...")
    alertas = []

    try:
        sheet = conectar_sheets()
        print("✅ Conectado a Google Sheets")

        df_eroshop, alertas_scraping = await scraping_eroshop()
        alertas.extend(alertas_scraping)

        df_exportar, resumen = procesar_cruce(df_eroshop, sheet)
        print(f"📊 Resumen: {resumen}")

        url_json = actualizar_gist(df_exportar)

        print("\n✅ Automatización completada exitosamente")
        print(f"   Total productos:  {resumen['total_productos']}")
        print(f"   Cambio precio:    {resumen['cambio_precio']}")
        print(f"   Cambio stock:     {resumen['cambio_stock']}")
        print(f"   Productos nuevos: {resumen['productos_nuevos']}")
        print(f"   A actualizar:     {resumen['a_actualizar']}")

        enviar_slack(
            f"✅ *Automatización Belove completada*\n"
            f"📦 Total productos: {resumen['total_productos']}\n"
            f"💰 Cambio precio: {resumen['cambio_precio']}\n"
            f"📊 Cambio stock: {resumen['cambio_stock']}\n"
            f"🆕 Productos nuevos: {resumen['productos_nuevos']}\n"
            f"🔄 A actualizar: {resumen['a_actualizar']}"
        )

        return {"status": "ok", "resumen": resumen, "alertas": alertas}

    except Exception as e:
        print(f"\n❌ Error: {e}")
        enviar_slack(f"❌ *Error en automatización Belove*\n{str(e)}")
        return {"status": "error", "mensaje": str(e)}

if __name__ == "__main__":
    result = asyncio.run(main())
    print(f"Resultado: {result}")
    print("Script terminado, esperando próxima ejecución...")
    while True:
        time.sleep(3600)
