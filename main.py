import asyncio
import os
import json
import random
import requests
import pandas as pd
from playwright.async_api import async_playwright
from google.oauth2.service_account import Credentials
import gspread

# ── CONFIGURACIÓN ─────────────────────────────────────────────
EMAIL          = os.environ.get("EROSHOP_EMAIL")
PASSWORD       = os.environ.get("EROSHOP_PASSWORD")
BASE_URL       = "https://www.eroshopmayorista.cl"
DELAY          = 2000
UMBRAL_CALIDAD = 0.10
GITHUB_TOKEN   = os.environ.get("GITHUB_TOKEN")
GIST_ID        = "c6a5fca73c46f6a98bef5f47dc8ff123"
SHEET_NAME     = "Belove - Automatización"
URL_BELOVE     = "https://belove.cl/ws/json_productos.php?token=WGRjRWs1WHU1dWdRZ1VCeHV0YVo="

# ── CONECTAR GOOGLE SHEETS ────────────────────────────────────
def conectar_sheets():
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive"
    ]
    import base64
    creds_raw = os.environ.get("GOOGLE_CREDENTIALS")
    creds_json = json.loads(base64.b64decode(creds_raw).decode())
    creds  = Credentials.from_service_account_info(creds_json, scopes=scope)
    client = gspread.authorize(creds)
    return client.open(SHEET_NAME)

# ── SCRAPING EROSHOP ──────────────────────────────────────────
async def crear_sesion(playwright):
    browser = await playwright.chromium.launch(headless=True)
    page = await browser.new_page()
    await page.goto(f"{BASE_URL}/customer/login")
    await page.fill("input[name='customer[email]']", EMAIL)
    await page.fill("input[name='customer[password]']", PASSWORD)
    await page.click("input[name='commit']")
    await page.wait_for_timeout(3000)
    if "login" in page.url:
        raise Exception("❌ Login fallido")
    print(f"✅ Login exitoso — {page.url}")
    return browser, page

async def extraer_producto(page, url):
    await page.goto(url)
    await page.wait_for_timeout(DELAY)

    nombre = ""
    el = await page.query_selector("h1")
    if el:
        nombre = (await el.inner_text()).strip()

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

    img = ""
    for selector in [".product-image img", "figure img", ".main-image img"]:
        el = await page.query_selector(selector)
        if el:
            img = await el.get_attribute("src") or ""
            if img:
                break

    return {"nombre": nombre, "sku": sku, "stock": stock, "precio_neto": precio, "url": url, "imagen": img}

async def scraping_eroshop():
    async with async_playwright() as pw:
        browser, page = await crear_sesion(pw)

        # Detectar páginas
        TOTAL_PAGINAS = 20
        print(f"📄 Total páginas: {TOTAL_PAGINAS}")

        # Recolectar URLs
        print(f"\n📋 Recorriendo {TOTAL_PAGINAS} páginas...")
        product_urls = []
        for pg in range(1, TOTAL_PAGINAS + 1):
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
            print(f"  Página {pg}/{TOTAL_PAGINAS} → acumulado: {len(product_urls)}")

        # Extraer datos
        print(f"\n🔍 Extrayendo {len(product_urls)} productos...")
        productos = []
        for i, url in enumerate(product_urls, 1):
            intentos = 0
            while intentos < 3:
                try:
                    dato = await extraer_producto(page, url)
                    productos.append(dato)
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

        # Validación calidad
        total = len(df)
        sin_sku    = df[df["sku"] == ""].shape[0]
        sin_precio = df[df["precio_neto"] == 0].shape[0]
        alertas = []
        if sin_sku / total > UMBRAL_CALIDAD:
            alertas.append(f"🚨 {sin_sku} productos sin SKU ({sin_sku/total:.0%})")
        if sin_precio / total > UMBRAL_CALIDAD:
            alertas.append(f"🚨 {sin_precio} productos sin precio ({sin_precio/total:.0%})")

        print(f"\n✅ Scraping completo: {total} productos")
        return df, alertas

# ── CRUCE Y EXPORTAR ──────────────────────────────────────────
def procesar_cruce(df_eroshop, sheet):
    # Belove
    df_belove = pd.DataFrame(requests.get(URL_BELOVE).json())
    df_belove["sku"] = df_belove["sku"].astype(str).str.strip()
    df_eroshop["sku"] = df_eroshop["sku"].astype(str).str.strip()

    # Costos especiales
    ws_costos = sheet.worksheet("costos_especiales")
    df_costos = pd.DataFrame(ws_costos.get_all_records())
    df_costos["sku"] = df_costos["sku"].astype(str).str.strip()

    # Agregar productos China
    skus_eroshop = set(df_eroshop["sku"])
    df_costos_nuevos = df_costos[~df_costos["sku"].isin(skus_eroshop)].copy()
    if len(df_costos_nuevos) > 0:
        df_china = df_costos_nuevos.merge(df_belove[["sku", "nombre", "stock"]], on="sku", how="left")
        df_china["precio_neto"] = 0
        df_china["origen"] = "china"
        df_china = df_china[["nombre", "sku", "stock", "precio_neto", "origen"]]
        df_eroshop["origen"] = "eroshop"
        df_eroshop = pd.concat([df_eroshop, df_china], ignore_index=True)

    # Merge
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
    df_raw = df_eroshop[["nombre", "sku", "stock", "precio_neto"]].fillna("")
    ws_raw = sheet.worksheet("eroshop_raw")
    ws_raw.clear()
    ws_raw.update(range_name="A1", values=[df_raw.columns.tolist()] + df_raw.values.tolist())

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
            row.get("sku",""), row.get("nombre",""), row.get("costo_neto",""),
            f"=IF(ISERROR(VLOOKUP(A{i};costos_especiales!$A:$B;2;0));ROUND(C{i}*config!$B$2;0);VLOOKUP(A{i};costos_especiales!$A:$B;2;0))",
            f"=FLOOR(D{i}*config!$B$3;1000)+990",
            row.get("precio_actual_belove",""), row.get("precio_descuento_belove",""),
            row.get("stock_eroshop",""), row.get("stock_belove",""),
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

    ws_cruce = sheet.worksheet("cruce")
    ws_cruce.clear()
    ws_cruce.update(range_name="A1", values=[encabezados] + filas, value_input_option="USER_ENTERED")

    # Leer cruce procesado y exportar
    data_cruce = ws_cruce.get_all_records()
    df_resultado = pd.DataFrame(data_cruce)

    df_actualizar = df_resultado[
        (df_resultado["cambio_precio"] == "SÍ") |
        (df_resultado["cambio_stock"] == "SÍ")
    ].copy()

    df_actualizar = df_actualizar.merge(df_belove[["id", "sku"]], on="sku", how="left")
    df_actualizar["id"] = pd.to_numeric(df_actualizar["id"], errors="coerce").fillna(0).astype(int)

    def calcular_precio(row):
        precio_descuento = int(row["precio_descuento"]) if row["precio_descuento"] else 0
        if row["producto_nuevo"] == "NUEVO":
            pct = random.uniform(0.15, 0.69)
            return int((precio_descuento * (1 + pct) // 1000) * 1000 + 990)
        return int(row["precio_actual_belove"]) if row["precio_actual_belove"] else precio_descuento

    df_actualizar["precio_final"] = df_actualizar.apply(calcular_precio, axis=1)
    df_actualizar["precio_descuento_final"] = df_actualizar["precio_descuento"].apply(lambda x: int(x) if x else 0)
    df_actualizar["stock_final"] = df_actualizar["stock_eroshop"].apply(lambda x: int(x) if x else 0)

    df_exportar = df_actualizar[["id", "sku", "precio_final", "precio_descuento_final", "stock_final"]].copy()
    df_exportar.columns = ["id", "sku", "precio", "precio_descuento", "stock"]
    df_exportar = df_exportar.fillna("")

    resumen = {
        "total_productos": len(df_resultado),
        "cambio_precio": int((df_resultado["cambio_precio"] == "SÍ").sum()),
        "cambio_stock": int((df_resultado["cambio_stock"] == "SÍ").sum()),
        "productos_nuevos": int((df_resultado["producto_nuevo"] == "NUEVO").sum()),
        "a_actualizar": len(df_exportar),
    }

    return df_exportar, resumen

# ── ACTUALIZAR GIST ───────────────────────────────────────────
def actualizar_gist(df_exportar):
    exportar_json = df_exportar.to_dict(orient="records")
    for item in exportar_json:
        for k, v in item.items():
            if str(v) in ["nan", ""]:
                item[k] = None

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
        # 1. Conectar Sheets
        sheet = conectar_sheets()
        print("✅ Conectado a Google Sheets")

        # 2. Scraping
        df_eroshop, alertas_scraping = await scraping_eroshop()
        alertas.extend(alertas_scraping)

        # 3. Cruce y exportar
        df_exportar, resumen = procesar_cruce(df_eroshop, sheet)
        print(f"📊 Resumen: {resumen}")

        # 4. Actualizar Gist
        url_json = actualizar_gist(df_exportar)

        print("\n✅ Automatización completada exitosamente")
        print(f"   Total productos: {resumen['total_productos']}")
        print(f"   Cambio precio:   {resumen['cambio_precio']}")
        print(f"   Cambio stock:    {resumen['cambio_stock']}")
        print(f"   Productos nuevos:{resumen['productos_nuevos']}")
        print(f"   A actualizar:    {resumen['a_actualizar']}")

        return {"status": "ok", "resumen": resumen, "alertas": alertas}

    except Exception as e:
        print(f"\n❌ Error: {e}")
        return {"status": "error", "mensaje": str(e)}

if __name__ == "__main__":
    asyncio.run(main())
