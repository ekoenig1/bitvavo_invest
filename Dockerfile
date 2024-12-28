FROM python:3.9-slim

# Setze Arbeitsverzeichnis
WORKDIR /app

# Kopiere die `requirements.txt` ins Image
COPY requirements.txt /app/requirements.txt

# Installiere Abhängigkeiten
RUN pip install --no-cache-dir -r requirements.txt

# Kopiere den Python-Programmcode ins Image
COPY bitmaster.py /app/bitmaster.py

# Exponiere Port 5000
EXPOSE 5000

# Setze den Startbefehl
CMD ["python", "bitmaster.py"]
