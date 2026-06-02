import asyncio
import os
import json
import random
import time
import requests
import pandas as pd
from datetime import datetime
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
DELAY          = 750
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
    return client, client.open(SHEET_NAME)

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
        # Producto con variantes
        variantes = []
        opciones = await selects[0].query_selector_all("option")

        for opcion in opciones:
            value = await opcion.get_attribute("value")
            if not value:
                continue

            await selects[0].select_option(value=value)
            await page.wait_for_timeout(1000)

            sku = ""
            el = await page.query_selector(".sku_elem")
            if el:
                sku = (await el.inner_text()).strip()

            stock_attr = await opcion.get_attribute("data-variant-stock")
            stock = stock_attr if stock_attr else "0"

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
                        print(f"  [{i}/{len(product_urls)}] {datos[0]['nombre'][:35]}")
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
        alertas = []
        if total > 0:
            sin_precio = df[df["precio_neto"] == 0].shape[0]
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
            f'=IF(ISERROR(VLOOKUP(TEXT(A{i};"0");TEXT(belove_raw!A:A;"0");1;0));"NUEVO";"EXISTE")',
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

    # Crear lookup de precios fijos China
    precios_fijos = {}
    if "precio_descuento_fijo" in df_costos.columns:
        for _, r in df_costos.iterrows():
            if r.get("precio_descuento_fijo") and int(r["precio_descuento_fijo"]) > 0:
                precios_fijos[str(r["sku"]).strip()] = int(r["precio_descuento_fijo"])

    # Calcular precio_descuento
    def calcular_precio_descuento(row):
        sku = str(row["sku"]).strip()
        if sku in precios_fijos:
            return precios_fijos[sku]
        return int(row["precio_descuento"]) if row["precio_descuento"] else 0

    # Calcular precio (siempre mayor que precio_descuento)
    def calcular_precio(row):
        precio_desc = row["precio_descuento_final"]
        if precio_desc == 0:
            return 0
        precio_actual_belove = int(row["precio_actual_belove"]) if row["precio_actual_belove"] else 0
        if row["producto_nuevo"] != "NUEVO" and precio_actual_belove > 0:
            return precio_actual_belove
        pct = random.uniform(0.15, 0.69)
        return int((precio_desc * (1 + pct) // 1000) * 1000 + 990)

    df_todos["precio_descuento_final"] = df_todos.apply(calcular_precio_descuento, axis=1)
    df_todos["precio_final"] = df_todos.apply(calcular_precio, axis=1)
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

    # ── DETECTAR PRODUCTOS NUEVOS CON STOCK ──────────────────
    # Comparar directamente contra df_belove en Python (más confiable que la fórmula de Sheets)
    skus_belove_norm = set(
        df_belove["sku"].astype(str).str.replace("-","").str.upper().str.lstrip("0").tolist()
    )

    nuevos_con_stock = df_resultado[
        (df_resultado["stock_eroshop"] > 0)
    ].copy()
    nuevos_con_stock = nuevos_con_stock[
        ~nuevos_con_stock["sku"].astype(str).str.replace("-","").str.upper().str.lstrip("0").isin(skus_belove_norm)
    ][["sku", "nombre", "stock_eroshop", "precio_calculado"]].copy()

    if len(nuevos_con_stock) > 0:
        lista = "\n".join([
            f"• {row['nombre'][:30]} | SKU: {row['sku']} | Stock: {int(row['stock_eroshop'])} | Precio: ${int(row['precio_calculado']):,}"
            for _, row in nuevos_con_stock.iterrows()
        ])
        enviar_slack(f"🆕 *{len(nuevos_con_stock)} productos nuevos en Eroshop con stock:*\n{lista}")

    # ── DETECTAR PRODUCTOS QUE DESAPARECIERON DE EROSHOP ─────
    # Normalizar SKUs de Eroshop para comparar con/sin guión
    skus_eroshop_norm = set(
        df_eroshop["sku"].astype(str).str.replace("-","").str.upper().str.lstrip("0").tolist()
    )

    productos_desaparecidos = df_belove[
        (~df_belove["sku"].astype(str).str.replace("-","").str.upper().str.lstrip("0").isin(skus_eroshop_norm)) &
        (df_belove["stock"] > 0)
    ][["id", "sku", "nombre", "stock"]].copy()

    # Excluir SKUs que están en costos_especiales (productos China que no están en Eroshop)
    skus_china = set(df_costos["sku"].astype(str).str.strip().tolist())
    productos_desaparecidos = productos_desaparecidos[
        ~productos_desaparecidos["sku"].astype(str).str.strip().isin(skus_china)
    ]

    if len(productos_desaparecidos) > 0:
        for _, row in productos_desaparecidos.iterrows():
            df_exportar = pd.concat([df_exportar, pd.DataFrame([{
                "id": int(row["id"]),
                "sku": row["sku"],
                "precio": 0,
                "precio_descuento": 0,
                "stock": 0
            }])], ignore_index=True)

        lista = "\n".join([
            f"• {row['nombre'][:30]} | SKU: {row['sku']} | Stock anterior: {int(row['stock'])}"
            for _, row in productos_desaparecidos.iterrows()
        ])
        enviar_slack(f"⚠️ *{len(productos_desaparecidos)} productos desaparecieron de Eroshop → stock 0:*\n{lista}")

    # ── HISTORIAL EN GOOGLE SHEETS ────────────────────────────
    try:
        ws_historial = sheet.worksheet("historial")
    except:
        ws_historial = sheet.add_worksheet(title="historial", rows=1000, cols=10)
        ws_historial.update(range_name="A1", values=[["fecha", "total_productos", "cambio_precio", "cambio_stock", "productos_nuevos", "desaparecidos", "a_actualizar"]])

    ws_historial.append_rows([[
        datetime.now().strftime("%Y-%m-%d %H:%M"),
        resumen["total_productos"],
        resumen["cambio_precio"],
        resumen["cambio_stock"],
        resumen["productos_nuevos"],
        len(productos_desaparecidos),
        resumen["a_actualizar"],
    ]])
    print("✅ Historial actualizado")

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

# ── ACTUALIZAR VENTAS NUEVAS ──────────────────────────────────
def actualizar_ventas(sheet_resultados, sheet_eroshop):
    import xml.etree.ElementTree as ET

    # Leer pedidos existentes
    ws_ventas = sheet_resultados.worksheet("ventas")
    df_ventas_actual = pd.DataFrame(ws_ventas.get_all_records())
    pedidos_existentes = set(df_ventas_actual["pedido_ID"].astype(str).tolist())
    print(f"📋 Pedidos en Sheets: {len(pedidos_existentes)}")

    # Descargar XML
    resp = requests.get("https://belove.cl/moldeable/exportarEliasXML.php")
    root = ET.fromstring(resp.content)
    print(f"📋 Pedidos en XML: {len(root.findall('pedido'))}")

    # Leer configuración
    ws_config = sheet_resultados.worksheet("costos_config")
    df_config = pd.DataFrame(ws_config.get_all_records())
    config = {r["parametro"]: float(str(r["valor"]).replace(",", ".")) for _, r in df_config.iterrows()}

    ws_comunas = sheet_resultados.worksheet("despacho_comunas")
    df_comunas = pd.DataFrame(ws_comunas.get_all_records())
    df_comunas["costo_despacho"] = pd.to_numeric(df_comunas["costo_despacho"], errors="coerce").fillna(4600)
    costo_despacho_comuna = dict(zip(
        df_comunas["comuna"].str.strip().str.upper(),
        df_comunas["costo_despacho"]
    ))

    ws_eroshop_raw = sheet_eroshop.worksheet("eroshop_raw")
    df_eroshop_raw = pd.DataFrame(ws_eroshop_raw.get_all_records())
    df_eroshop_raw["sku"] = df_eroshop_raw["sku"].astype(str).str.strip()
    df_eroshop_raw["precio_neto"] = pd.to_numeric(df_eroshop_raw["precio_neto"], errors="coerce").fillna(0)
    costos_eroshop = dict(zip(df_eroshop_raw["sku"], df_eroshop_raw["precio_neto"]))

    df_belove = pd.DataFrame(requests.get(URL_BELOVE).json())
    df_belove["sku"] = df_belove["sku"].astype(str).str.strip()
    nombres_belove = dict(zip(df_belove["sku"], df_belove["nombre"]))

    ESTADOS_VALIDOS  = {"Pagado", "acreditado"}
    BOLSA_DENTRO     = config.get("bolsa_dentro", 160)
    BOLSA_FUERA      = config.get("bolsa_fuera", 70)
    STICKER_LARGO    = config.get("sticker_largo", 140)
    STICKER_CIRC     = config.get("sticker_circulo", 51)
    raw_comision     = config.get("comision_pago", 300)
    COMISION         = raw_comision / 100000
    DESPACHO_DESDE   = config.get("despacho_gratis_desde", 40000)
    REGALO_DESDE     = config.get("regalo_desde", 120000)

    seen_items = set()
    nuevas_ventas = []
    nuevos_items = []

    for pedido in root.findall("pedido"):
        estado = pedido.findtext("Estado", "")
        if estado not in ESTADOS_VALIDOS:
            continue
        pedido_id = pedido.findtext("ID", "")
        if pedido_id in pedidos_existentes:
            continue

        total        = float(pedido.findtext("Total_pagado", 0) or 0)
        despacho_val = float(pedido.findtext("Valor_despacho", 0) or 0)
        region       = pedido.findtext("Region", "")
        comuna       = pedido.findtext("Comuna", "")
        fecha        = pedido.findtext("Fecha", "")
        medio_pago   = pedido.findtext("Medio_de_pago", "")
        descuento    = float(pedido.findtext("Descuento", 0) or 0)
        oc           = pedido.findtext("OC", "")

        costo_despacho_real = costo_despacho_comuna.get(comuna.strip().upper(), 4600)
        despacho_costo = costo_despacho_real if total > DESPACHO_DESDE else 0
        regalo         = 1 if total > REGALO_DESDE else 0
        costo_bolsas   = BOLSA_DENTRO + BOLSA_FUERA
        costo_stickers = STICKER_LARGO + STICKER_CIRC
        costo_comision = round(total * COMISION)

        costo_eroshop_pedido = 0
        productos_pedido = []
        seen_en_pedido = set()

        for prod in pedido.findall(".//producto"):
            item_id = prod.findtext("ID", "")
            clave = (pedido_id, item_id)
            if clave in seen_items:
                continue
            seen_items.add(clave)
            if item_id in seen_en_pedido:
                continue
            seen_en_pedido.add(item_id)

            sku            = prod.findtext("Codigo", "").strip()
            nombre         = nombres_belove.get(sku, prod.findtext("Nombre", ""))
            cantidad       = float(prod.findtext("Cantidad", 1) or 1)
            precio_unit    = float(prod.findtext("Precio_unitario", 0) or 0)
            precio_total_p = float(prod.findtext("Precio_total", 0) or 0)
            costo_unit     = costos_eroshop.get(sku, round(precio_unit * 0.5))
            costo_eroshop_pedido += costo_unit * cantidad

            productos_pedido.append({
                "pedido_ID": pedido_id, "fecha": fecha, "sku": sku,
                "nombre_producto": nombre, "cantidad": int(cantidad),
                "precio_unitario": int(precio_unit), "precio_total": int(precio_total_p),
                "costo_producto": int(costo_unit), "costo_confiable": 1
            })

        nuevos_items.extend(productos_pedido)
        costo_total  = costo_eroshop_pedido + despacho_costo + costo_bolsas + costo_stickers + costo_comision
        rentabilidad = total - costo_total
        pct_rent     = round(rentabilidad / total * 100, 1) if total > 0 else 0

        nuevas_ventas.append({
            "pedido_ID": pedido_id, "OC": oc, "fecha": fecha, "estado": estado,
            "medio_pago": medio_pago, "region": region, "comuna": comuna,
            "total": int(total), "valor_despacho": int(despacho_val),
            "descuento": int(descuento), "cantidad_productos": len(productos_pedido),
            "regalo": regalo, "costo_eroshop": int(costo_eroshop_pedido),
            "despacho_costo": int(despacho_costo), "costo_bolsas": int(costo_bolsas),
            "costo_stickers": int(costo_stickers), "costo_comision": int(costo_comision),
            "costo_total": int(costo_total), "rentabilidad": int(rentabilidad),
            "pct_rentabilidad": pct_rent, "costo_confiable": 1
        })

    print(f"📊 Ventas nuevas: {len(nuevas_ventas)} | Items nuevos: {len(nuevos_items)}")

    if nuevas_ventas:
        ws_ventas.append_rows(pd.DataFrame(nuevas_ventas).values.tolist())
        sheet_resultados.worksheet("items").append_rows(pd.DataFrame(nuevos_items).values.tolist())
        print(f"✅ {len(nuevas_ventas)} ventas y {len(nuevos_items)} items agregados")
        return len(nuevas_ventas)
    else:
        print("✅ Ventas al día")
        return 0

# ── ACTUALIZAR RESUMEN MES ACTUAL ─────────────────────────────
def actualizar_resumen_mes_actual(sheet_resultados):
    from datetime import datetime
    import math

    ahora = datetime.now()
    año_actual = ahora.year
    mes_actual = ahora.month
    MESES_N = ['Ene','Feb','Mar','Abr','May','Jun','Jul','Ago','Sep','Oct','Nov','Dic']
    periodo_actual = f"{MESES_N[mes_actual-1]} {año_actual}"

    print(f"📅 Calculando resumen para: {periodo_actual}")

    # Leer ventas
    ws_ventas = sheet_resultados.worksheet("ventas")
    df_v = pd.DataFrame(ws_ventas.get_all_records())
    if df_v.empty:
        print("⚠️ Sin ventas"); return

    df_v["fecha"] = pd.to_datetime(df_v["fecha"], errors="coerce")
    df_v["total"] = pd.to_numeric(df_v["total"], errors="coerce").fillna(0)
    df_v["cantidad_productos"] = pd.to_numeric(df_v["cantidad_productos"], errors="coerce").fillna(1)

    estados_validos = {"Pagado", "acreditado"}
    df_mes = df_v[
        (df_v["fecha"].dt.year == año_actual) &
        (df_v["fecha"].dt.month == mes_actual) &
        (df_v["estado"].isin(estados_validos))
    ].copy()

    if df_mes.empty:
        print(f"⚠️ Sin ventas para {periodo_actual}"); return

    # Leer configuración costos fijos
    ws_config = sheet_resultados.worksheet("costos_config")
    df_config = pd.DataFrame(ws_config.get_all_records())
    config = {r["parametro"]: float(str(r["valor"]).replace(",",".")) for _, r in df_config.iterrows()}

    # Costos fijos del mes (misma lógica que resumen_mensual)
    uber      = float(config.get("uber", 0))
    etpay     = float(config.get("etpay", 0))
    contador  = float(config.get("contador", 0))
    banco     = float(config.get("banco", 0))
    adwords   = float(config.get("adwords", 0))
    otros     = float(config.get("otros", 0))
    total_fijos = uber + etpay + contador + banco + adwords + otros

    # Si no hay config de fijos, usar el valor conocido
    if total_fijos == 0:
        total_fijos = 1445010

    # Métricas del mes
    pedidos   = len(df_mes)
    ingresos  = int(df_mes["total"].sum())
    ticket    = round(ingresos / pedidos) if pedidos > 0 else 0

    # Costos variables
    df_mes["costo_eroshop"]  = pd.to_numeric(df_mes.get("costo_eroshop", 0), errors="coerce").fillna(0)
    df_mes["despacho_costo"] = pd.to_numeric(df_mes.get("despacho_costo", 0), errors="coerce").fillna(0)
    df_mes["costo_bolsas"]   = pd.to_numeric(df_mes.get("costo_bolsas", 0), errors="coerce").fillna(0)
    df_mes["costo_stickers"] = pd.to_numeric(df_mes.get("costo_stickers", 0), errors="coerce").fillna(0)
    df_mes["costo_comision"] = pd.to_numeric(df_mes.get("costo_comision", 0), errors="coerce").fillna(0)

    costo_eroshop  = int(df_mes["costo_eroshop"].sum())
    costo_despacho = int(df_mes["despacho_costo"].sum())
    costo_bolsas   = int(df_mes["costo_bolsas"].sum())
    costo_stickers = int(df_mes["costo_stickers"].sum())
    costo_comision = int(df_mes["costo_comision"].sum())
    costo_variable_total = costo_eroshop + costo_despacho + costo_bolsas + costo_stickers + costo_comision

    rent_variable = ingresos - costo_variable_total
    pct_rv = round(rent_variable / ingresos * 100, 2) if ingresos > 0 else 0
    rent_real = rent_variable - total_fijos
    pct_rr = round(rent_real / ingresos * 100, 2) if ingresos > 0 else 0

    nueva_fila = [
        periodo_actual,       # A: periodo
        año_actual,           # B: año
        mes_actual,           # C: mes
        pedidos,              # D: pedidos
        ingresos,             # E: ingresos
        ticket,               # F: ticket_promedio
        costo_eroshop,        # G: costo_eroshop
        costo_despacho,       # H: costo_despacho
        costo_bolsas,         # I: costo_bolsas
        costo_stickers,       # J: costo_stickers
        costo_comision,       # K: costo_comision
        costo_variable_total, # L: costo_variable_total
        rent_variable,        # M: rentabilidad_variable
        pct_rv,               # N: pct_rent_variable
        uber,                 # O: uber
        etpay,                # P: etpay
        contador,             # Q: contador
        banco,                # R: banco
        adwords,              # S: adwords
        otros,                # T: otros
        int(total_fijos),     # U: total_fijos
        rent_real,            # V: rentabilidad_real
        pct_rr,               # W: pct_rent_real
    ]

    # Buscar si ya existe el mes en resumen_mensual
    ws_resumen = sheet_resultados.worksheet("resumen_mensual")
    data = ws_resumen.get_all_values()
    headers = data[0] if data else []

    fila_existente = None
    for i, fila in enumerate(data[1:], start=2):
        if fila and str(fila[0]).replace('.0','').strip() == periodo_actual:
            fila_existente = i
            break

    if fila_existente:
        # Actualizar fila existente
        ws_resumen.update(f"A{fila_existente}:W{fila_existente}", [nueva_fila])
        print(f"✅ resumen_mensual actualizado — fila {fila_existente}: {periodo_actual}")
    else:
        # Agregar nueva fila al final
        ws_resumen.append_rows([nueva_fila])
        print(f"✅ resumen_mensual — nueva fila agregada: {periodo_actual}")

    # Regenerar resumen_completo
    regenerar_resumen_completo(sheet_resultados)


def regenerar_resumen_completo(sheet_resultados):
    MESES_N = ['Ene','Feb','Mar','Abr','May','Jun','Jul','Ago','Sep','Oct','Nov','Dic']
    FIJOS   = 1445010

    ws_ventas  = sheet_resultados.worksheet("ventas")
    ws_resumen = sheet_resultados.worksheet("resumen_mensual")

    # Leer ventas para 2022-2024
    df_v = pd.DataFrame(ws_ventas.get_all_records())
    df_v["fecha"] = pd.to_datetime(df_v["fecha"], errors="coerce")
    df_v["total"] = pd.to_numeric(df_v["total"], errors="coerce").fillna(0)
    estados_validos = {"Pagado", "acreditado"}
    df_v = df_v[df_v["estado"].isin(estados_validos) & df_v["fecha"].notna()]
    df_v = df_v[df_v["fecha"].dt.year < 2025]

    meses_hist = {}
    for _, r in df_v.iterrows():
        key = (r["fecha"].year, r["fecha"].month)
        if key not in meses_hist:
            meses_hist[key] = {"pedidos":0,"ingresos":0}
        meses_hist[key]["pedidos"]  += 1
        meses_hist[key]["ingresos"] += r["total"]

    rows = []
    for (año, mes) in sorted(meses_hist.keys()):
        m = meses_hist[(año,mes)]
        ing = m["ingresos"]
        ped = m["pedidos"]
        if ing <= 0: continue
        tk       = round(ing/ped) if ped>0 else 0
        rv       = ing * 0.50
        pct_rv   = 50.0
        rr       = rv - FIJOS
        pct_rr   = round(rr/ing*100, 2) if ing>0 else 0
        periodo  = f"{MESES_N[mes-1]} {año}"
        rows.append([periodo, año, mes, ped, int(ing), tk,
                     int(rv), pct_rv, FIJOS, int(rr), pct_rr, True])

    # Leer resumen_mensual (2025+)
    data_r = ws_resumen.get_all_values()
    for fila in data_r[1:]:
        if not fila or not fila[0]: continue
        try:
            ing = float(fila[4]) if len(fila)>4 else 0
            if ing <= 0: continue
            periodo = str(fila[0]).replace('.0','').strip()
            año     = int(float(fila[1])) if fila[1] else 0
            mes     = int(float(fila[2])) if fila[2] else 0
            ped     = int(float(fila[3])) if fila[3] else 0
            tk      = int(float(fila[5])) if len(fila)>5 and fila[5] else 0
            rv      = int(float(fila[12])) if len(fila)>12 and fila[12] else 0
            pct_rv  = float(fila[13]) if len(fila)>13 and fila[13] else 0
            tf      = int(float(fila[20])) if len(fila)>20 and fila[20] else FIJOS
            rr      = int(float(fila[21])) if len(fila)>21 and fila[21] else 0
            pct_rr  = float(fila[22]) if len(fila)>22 and fila[22] else 0
            rows.append([periodo, año, mes, ped, int(ing), tk,
                         rv, pct_rv, tf, rr, pct_rr, False])
        except Exception as e:
            print(f"⚠️ Error fila resumen_mensual: {e}")
            continue

    # Escribir resumen_completo
    try:
        rc = sheet_resultados.worksheet("resumen_completo")
        rc.clear()
    except:
        rc = sheet_resultados.add_worksheet("resumen_completo", rows=200, cols=15)

    headers = ["periodo","año","mes","pedidos","ingresos","ticket_promedio",
               "rentabilidad_variable","pct_rent_variable","total_fijos",
               "rentabilidad_real","pct_rent_real","estimado"]
    rc.update("A1", [headers] + rows)
    print(f"✅ resumen_completo regenerado — {len(rows)} meses")

# ── MAIN ──────────────────────────────────────────────────────
async def main():
    print("🚀 Iniciando automatización Belove...")
    alertas = []

    try:
        client, sheet = conectar_sheets()
        print("✅ Conectado a Google Sheets")
        # Conectar sheet de resultados
        SHEET_RESULTADOS = "1Hm3O2bh1iZvJLdSfqoHTj5U82EBMCRcvQ0dsy3LtLNk"
        sheet_resultados = client.open_by_key(SHEET_RESULTADOS)
        ventas_nuevas = actualizar_ventas(sheet_resultados, sheet)
        # Actualizar resumen del mes actual
        actualizar_resumen_mes_actual(sheet_resultados)
        if ventas_nuevas > 0:
            enviar_slack(f"💰 *{ventas_nuevas} ventas nuevas registradas en Belove Resultados*")

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
    while True:
        result = asyncio.run(main())
        print(f"Resultado: {result}")

        ahora = datetime.now()
        manana_8am = ahora.replace(hour=9, minute=0, second=0, microsecond=0)
        if ahora.hour >= 8:
            import datetime as dt
            manana_8am = manana_8am + dt.timedelta(days=1)

        segundos = (manana_8am - ahora).total_seconds()
        horas = segundos / 3600
        print(f"⏰ Próxima ejecución: {manana_8am.strftime('%Y-%m-%d')} 05:00 Chile (en {horas:.1f} horas)")
        time.sleep(segundos)
