# FacturaNova 1.0

Facturación para novacomp.

Permite iniciar sesión con cuentas y roles, crear facturas privadas por facturador, ver el desglose de subtotal/impuesto/total, duplicar, editar, eliminar con confirmación y descargar el PDF. El administrador ve todas las facturas y puede gestionar usuarios. El diseño del PDF sigue la plantilla proporcionada. No certifica requisitos fiscales ni de facturación electrónica.

## Guía de usuario

Consulta la [guía de usuario](./GUIA_DE_USUARIO.md) para ver instrucciones paso a paso y capturas de pantalla.
Para preparar el proyecto desde cero en otro equipo, consulta el [manual técnico de instalación local en Windows (PDF)](./docs/MANUAL_TECNICO_INSTALACION.pdf).

## Ejecutar en Windows

Necesitas Python 3.10 o posterior.

```powershell
cd facturas_web
$Python = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
& $Python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:FLASK_SECRET_KEY = & $Python -c "import secrets; print(secrets.token_hex(32))"
$env:APP_PASSWORD_HASH = & $Python -c "from getpass import getpass; from werkzeug.security import generate_password_hash; print(generate_password_hash(getpass('Nueva contraseña: ')))"
$env:APP_USERNAME = "admin"
& $Python app.py
```

Abre `http://127.0.0.1:5000`. La base de datos y los PDFs temporales se guardan en `instance`.
Si instalaste otra versión o en otra carpeta, ajusta la ruta de `$Python`.

El primer inicio crea al administrador usando `APP_USERNAME` (por defecto `admin`) y `APP_PASSWORD_HASH`. Inicia sesión con esa cuenta y abre **Usuarios** para crear facturadores o administradores, cambiar roles, restablecer contraseñas o desactivar cuentas. Usa contraseñas de al menos 10 caracteres. Para añadir usuarios no hay registro público.

Cada facturador solo puede ver y gestionar las facturas que creó. El administrador ve todas, incluidas las facturas anteriores a la instalación del mantenimiento de usuarios. Las cuentas con facturas no se pueden borrar; desactívalas para conservar el historial. La aplicación agrega automáticamente el identificador de propietario a la base existente sin borrar las facturas.

Puedes configurar los datos habituales del emisor para que aparezcan prellenados al crear facturas:

```powershell
$env:SELLER_NAME = "Nombre del emisor"
$env:SELLER_TAX_ID = "Número de identificación"
$env:SELLER_PHONE = "Teléfono"
$env:SELLER_ADDRESS = "Dirección"
```

También puedes escribirlos directamente en el formulario. La plantilla no importa automáticamente datos personales del PDF de referencia.

## Publicar en Render

1. Crea un repositorio Git privado y agrega el contenido de esta carpeta. No publiques `instance`, archivos `.env` ni contraseñas.
2. En Render, crea un **Web Service** conectado al repositorio.
3. Configura:
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `gunicorn app:app`
   - **Environment:** `Python`
4. Añade variables de entorno:
   - `FLASK_ENV` = `production`
   - `FLASK_SECRET_KEY` = valor aleatorio largo (generado con `& $Python -c "import secrets; print(secrets.token_hex(32))"`)
   - `APP_USERNAME` = el usuario que usarás para entrar
   - `APP_PASSWORD_HASH` = hash generado localmente con el comando siguiente
   - `DATABASE_PATH` = `/var/data/facturas.sqlite3`
   - `SELLER_NAME`, `SELLER_TAX_ID`, `SELLER_PHONE`, `SELLER_ADDRESS` = datos del emisor para prellenar el formulario (opcionales)
   El usuario y hash configurados crean el primer administrador si la base no tiene cuentas. El administrador podrá crear las cuentas del equipo desde la opción **Usuarios**.
5. Añade un **Persistent Disk** al servicio, montado en `/var/data`, y despliega. Render puede requerir un plan de pago para usar discos persistentes. Sin disco persistente, la información guardada puede perderse al reiniciar o desplegar.
6. Entra en la URL HTTPS que Render asigne. No compartas la URL junto con las credenciales.

Para generar el hash de la contraseña en PowerShell, ejecuta desde la carpeta del proyecto:

```powershell
$env:APP_PASSWORD_HASH = & $Python -c "from getpass import getpass; from werkzeug.security import generate_password_hash; print(generate_password_hash(getpass('Nueva contraseña: ')))"
```

Copia el resultado como `APP_PASSWORD_HASH` en Render. No uses una contraseña débil ni pegues el texto de la contraseña en el repositorio.

### Recuperar la contraseña del administrador existente

`APP_PASSWORD_HASH` solo crea la cuenta inicial cuando la base de datos aún no tiene usuarios; no reemplaza la contraseña de una cuenta existente. Para restablecerla sin borrar facturas:

1. Genera un token temporal aleatorio en PowerShell: `& $Python -c "import secrets; print(secrets.token_urlsafe(32))"`.
2. Añade ese valor como `ADMIN_RECOVERY_TOKEN` en las variables de entorno de Render y guarda/despliega el servicio. No lo compartas ni lo agregues al repositorio.
3. Cuando el servicio esté **Live**, abre `https://TU-SERVICIO.onrender.com/admin/recovery`, introduce el token y establece una contraseña nueva de al menos 10 caracteres para el usuario configurado en `APP_USERNAME` (por defecto, `admin`).
4. Al confirmar el mensaje de éxito, elimina `ADMIN_RECOVERY_TOKEN` de Render y vuelve a desplegar. El token se registra como usado en la base, y la ruta deja de estar disponible al retirar la variable.

La recuperación conserva las facturas y solo funciona con un token no utilizado. La cuenta objetivo debe existir, estar activa y tener rol de administrador.
