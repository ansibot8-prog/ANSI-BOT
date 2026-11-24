# Netflix Cookie Checker Bot

Bot de Telegram para validar cookies de Netflix y obtener información de la cuenta.

## Características
- Soporte multi-formato de cookies.
- Generación de tokens de sesión (nftoken).
- Extracción de datos de cuenta (plan, perfiles, país, etc.).
- Rate limiting integrado para evitar bloqueos.

## Instalación Local
1. Clona el repo.
2. Crea un archivo `.env` con `BOT_TOKEN=tu_token_aqui`.
3. Instala dependencias: `pip install -r requirements.txt`.
4. Ejecuta: `python bot.py`.

## Despliegue en Render
1. Conecta este repo a Render.
2. Añade la variable de entorno `BOT_TOKEN` en el dashboard de Render.
3. Deploy automático.
