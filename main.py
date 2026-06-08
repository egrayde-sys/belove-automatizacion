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
    # Crear lookup de stock fijo
    stocks_fijos = {}
    if "stock_fijo" in df_costos.columns:
        for _, r in df_costos.iterrows():
            val = r.get("stock_fijo", "")
            if val != "" and pd.to_numeric(val, errors="coerce") > 0:
                stocks_fijos[str(r["sku"]).strip()] = int(val)

    # Stock final — usa stock_fijo si existe, sino stock de Eroshop
    def calcular_stock(row):
        sku = str(row["sku"]).strip()
        if sku in stocks_fijos:
            return stocks_fijos[sku]
        return int(row["stock_eroshop"]) if row["stock_eroshop"] else 0

    df_todos["stock_final"] = df_todos.apply(calcular_stock, axis=1)

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

    # ── PROCESAR PACKS ────────────────────────────────────────
    try:
        ws_packs = sheet.worksheet("packs")
        df_packs = pd.DataFrame(ws_packs.get_all_records())
        df_packs = df_packs[df_packs["activo"].astype(str) == "1"].copy()
        print(f"DEBUG packs activos: {len(df_packs)}")

        # Lookup de precio y stock desde df_belove y df_resultado
        precio_por_sku = {}
        precio_desc_por_sku = {}
        stock_por_sku = {}

        for _, r in df_belove.iterrows():
            sku = str(r["sku"]).strip()
            precio_por_sku[sku] = int(r["precio"]) if r["precio"] else 0
            precio_desc_por_sku[sku] = int(r["precio_descuento"]) if r["precio_descuento"] else 0
            stock_por_sku[sku] = int(r["stock"]) if r["stock"] else 0

        # Actualizar con stock fresco de Eroshop
        for _, r in df_eroshop.iterrows():
            sku = str(r["sku"]).strip()
            stock_por_sku[sku] = int(r["stock"]) if r["stock"] else 0

        # Sobreescribir con stock_fijo de costos_especiales (tiene prioridad)
        for _, r in df_costos.iterrows():
            sku = str(r["sku"]).strip()
            val = r.get("stock_fijo", "")
            if val != "" and pd.to_numeric(val, errors="coerce") >= 0:
                stock_por_sku[sku] = int(pd.to_numeric(val, errors="coerce"))

        packs_resultado = []
        packs_sin_stock = []
        packs_stock_bajo = []

        for _, pack in df_packs.iterrows():
            pack_sku = str(pack["pack_sku"]).strip()
            nombre   = str(pack["nombre_pack"]).strip()
            skus     = [str(pack.get(f"sku_{i}", "")).strip() for i in range(1, 4) if str(pack.get(f"sku_{i}", "")).strip()]
            descuento_pct = float(pack.get("descuento_pct", 12)) / 100

            # Buscar ID del pack en Belove
            pack_id = buscar_id_belove(pack_sku)

            # Calcular stock — mínimo entre componentes
            stocks = [stock_por_sku.get(s, 0) for s in skus]
            stock_pack = min(stocks) if stocks else 0

            # Calcular precios
            precio_lista = sum(precio_por_sku.get(s, 0) for s in skus)
            precio_desc_base = sum(precio_desc_por_sku.get(s, 0) for s in skus)
            precio_desc_pack = int(precio_desc_base * (1 - descuento_pct))
            # Redondear a 990
            precio_desc_pack = int(precio_desc_pack // 1000) * 1000 + 990
            precio_lista_pack = int(precio_lista // 1000) * 1000 + 990

            packs_resultado.append({
                "id": pack_id,
                "sku": pack_sku,
                "precio": precio_lista_pack,
                "precio_descuento": precio_desc_pack,
                "stock": stock_pack
            })

            # Clasificar para Slack
            componentes_info = " + ".join([f"SKU {s} (stock:{stock_por_sku.get(s,0)})" for s in skus])
            if stock_pack == 0:
                packs_sin_stock.append(f"🔴 {nombre} ({pack_sku}) — {componentes_info}")
            elif stock_pack <= 3:
                packs_stock_bajo.append(f"🟡 {nombre} ({pack_sku}) — stock:{stock_pack} — {componentes_info}")

        # Agregar packs al exportar
        df_packs_export = pd.DataFrame(packs_resultado)
        df_exportar = pd.concat([df_exportar, df_packs_export], ignore_index=True)
        print(f"DEBUG packs procesados: {len(packs_resultado)}")

        # Slack packs — resumen completo
        msg_packs = f"📦 *Resumen Packs ({len(packs_resultado)} activos):*\n"

        if packs_sin_stock:
            msg_packs += f"\n🔴 *Sin stock ({len(packs_sin_stock)}):*\n" + "\n".join(packs_sin_stock)
        if packs_stock_bajo:
            msg_packs += f"\n🟡 *Stock bajo ({len(packs_stock_bajo)}):*\n" + "\n".join(packs_stock_bajo)

        # Resumen de todos los packs
        msg_packs += f"\n\n📋 *Detalle completo:*\n"
        for p in packs_resultado:
            sku = p["sku"]
            nombre_pack = next((str(pk["nombre_pack"]) for _, pk in df_packs.iterrows() if str(pk["pack_sku"]).strip() == sku), sku)
            emoji = "🔴" if p["stock"] == 0 else "🟡" if p["stock"] <= 3 else "✅"
            msg_packs += f"{emoji} {nombre_pack} — stock:{p['stock']} | desc:${p['precio_descuento']:,} | lista:${p['precio']:,}\n"

        enviar_slack(msg_packs)

    except Exception as e:
        import traceback
        print(f"⚠️ Error procesando packs: {e}")
        print(traceback.format_exc())
    
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
        client, sheet = conectar_sheets()
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
