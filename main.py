import os
import time

print("🚀 Container iniciado")
print(f"Python funcionando")
print(f"Variables de entorno:")
print(f"  EROSHOP_EMAIL: {'OK' if os.environ.get('EROSHOP_EMAIL') else 'NONE'}")
print(f"  GITHUB_TOKEN: {'OK' if os.environ.get('GITHUB_TOKEN') else 'NONE'}")
print(f"  GOOGLE_CREDENTIALS: {'OK longitud='+str(len(os.environ.get('GOOGLE_CREDENTIALS',''))) if os.environ.get('GOOGLE_CREDENTIALS') else 'NONE'}")
print("✅ Test completado")

while True:
    time.sleep(3600)
