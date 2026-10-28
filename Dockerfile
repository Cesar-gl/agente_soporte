# Imagen base: Linux mínimo con Python 3.12
FROM python:3.12-slim

# Mostrar los print() al instante (logs en vivo)
ENV PYTHONUNBUFFERED=1

# Carpeta de trabajo dentro del contenedor
WORKDIR /app

# Primero las dependencias (capa cacheada)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Después el script
COPY agente.py .

# Comando por defecto (se le pueden añadir args como --una-vez)
ENTRYPOINT ["python", "agente.py"]