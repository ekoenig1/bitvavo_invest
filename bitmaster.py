import os
import time
import datetime
import threading
import schedule
import sqlite3
import logging
import smtplib

from datetime import timedelta
from flask import (
    Flask, request, render_template_string, redirect,
    url_for, flash, session, get_flashed_messages
)
from python_bitvavo_api.bitvavo import Bitvavo
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

########################################
# 1) Logging konfigurieren
#    -> Logging in eine Datei und in die Konsole.
########################################
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.FileHandler("bitmaster.log"),     # Log in Datei
        logging.StreamHandler()                   # Zusätzlich in Konsole
    ]
)

########################################
# 2) Flask-App
########################################
app = Flask(__name__)

# SECRET_KEY und MASTER_PASSWORD über ENV-Variablen einstellbar
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "SUPER_GEHEIM_FUER_SESSION")
MASTER_PASSWORD = os.environ.get("MASTER_PASSWORD", "bitmaster")

# SIMULATION_MODE kann z.B. mit export SIMULATION_MODE=true aktiviert werden
SIMULATION_MODE = os.environ.get("SIMULATION_MODE", "false").lower() in ["true", "1", "yes"]

DB_NAME = "bitmaster.db"
print("DB-Pfad:", os.path.abspath(DB_NAME))


# Beispielhafte Liste an Assets, die man im Dropdown anbieten kann
ALLOWED_ASSETS = ["BTC", "ETH", "ADA", "XRP", "DOT", "SOL"]

########################################
# 3) DB-Funktionen mit Context Manager
########################################
def init_db():
    with sqlite3.connect(DB_NAME) as conn:
        c = conn.cursor()

        # Tabelle credentials (API-Keys)
        c.execute("""
        CREATE TABLE IF NOT EXISTS credentials (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            api_key TEXT,
            api_secret TEXT
        )
        """)

        # Tabelle schedules (Planung)
        c.execute("""
        CREATE TABLE IF NOT EXISTS schedules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            weekday TEXT,
            time_of_day TEXT
        )
        """)

        # schedule_lines (Detailzeilen je Schedule)
        c.execute("""
        CREATE TABLE IF NOT EXISTS schedule_lines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            schedule_id INTEGER,
            asset TEXT,
            amount_eur REAL
        )
        """)

        # Trades (abgeschlossene Käufe)
        c.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME,
            asset TEXT,
            amount_eur REAL,
            filled_asset REAL,
            avg_price REAL,
            order_id TEXT
        )
        """)

        # balances (Kontostands-Snapshots)
        c.execute("""
        CREATE TABLE IF NOT EXISTS balances (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME,
            currency TEXT,
            amount REAL
        )
        """)

        # historical_rates (historische Kurse)
        c.execute("""
        CREATE TABLE IF NOT EXISTS historical_rates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date DATE,
            asset TEXT,
            price_eur REAL
        )
        """)

        # E-Mail-Einstellungen
        c.execute("""
        CREATE TABLE IF NOT EXISTS email_settings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            smtp_server TEXT,
            smtp_port INTEGER,
            smtp_user TEXT,
            smtp_pass TEXT,
            from_email TEXT,
            to_email TEXT,
            send_on_success INTEGER,
            send_on_error INTEGER,
            use_tls INTEGER
        )
        """)


def get_connection():
    return sqlite3.connect(DB_NAME)


########################################
# 4) Einfache Authentifizierung
########################################
@app.before_request
def require_login():
    """
    Blockt alle Seiten bis auf /login und /do_login, falls nicht eingeloggt.
    """
    allowed_paths = ["/login", "/do_login", "/static"]
    if not session.get("logged_in") and not request.path.startswith(tuple(allowed_paths)):
        return redirect(url_for("login"))


@app.route("/login", methods=["GET"])
def login():
    html = """
    <html>
    <body>
      <h1>Login</h1>
      <form method="POST" action="{{ url_for('do_login') }}">
        <label>Passwort:</label>
        <input type="password" name="password">
        <button type="submit">Login</button>
      </form>
    </body>
    </html>
    """
    return render_template_string(html)


@app.route("/do_login", methods=["POST"])
def do_login():
    pw = request.form.get("password", "")
    if pw == MASTER_PASSWORD:
        session["logged_in"] = True
        logging.info("Login erfolgreich.")
        return redirect(url_for("index"))
    else:
        logging.warning("Falsches Passwort beim Login.")
        return "Falsches Passwort!"


########################################
# 5) E-Mail-Einstellungen
########################################
def load_email_settings():
    """
    Lädt die E-Mail-Einstellungen aus der DB (letzter Eintrag).
    Gibt ein Dict oder None zurück, wenn nichts gespeichert.
    """
    with get_connection() as conn:
        c = conn.cursor()
        c.execute("""
            SELECT smtp_server, smtp_port, smtp_user, smtp_pass, from_email, to_email,
                   send_on_success, send_on_error, use_tls
            FROM email_settings
            ORDER BY id DESC
            LIMIT 1
        """)
        row = c.fetchone()

    if row:
        return {
            "smtp_server":     row[0],
            "smtp_port":       row[1],
            "smtp_user":       row[2],
            "smtp_pass":       row[3],
            "from_email":      row[4],
            "to_email":        row[5],
            "send_on_success": bool(row[6]),
            "send_on_error":   bool(row[7]),
            "use_tls":         bool(row[8]),
        }
    else:
        return None


def send_email(subject, body):
    """
    Sendet eine E-Mail mit den in der DB gespeicherten SMTP-Einstellungen.
    Nutzt ggf. STARTTLS (Port 587), wenn 'use_tls' konfiguriert ist.
    """
    settings = load_email_settings()
    if not settings:
        logging.warning("send_email aufgerufen, aber keine E-Mail-Einstellungen konfiguriert.")
        return

    try:
        msg = MIMEMultipart()
        msg["From"] = settings["from_email"]
        msg["To"]   = settings["to_email"]
        msg["Subject"] = subject

        msg.attach(MIMEText(body, "plain"))

        # SMTP verbinden
        server = smtplib.SMTP(settings["smtp_server"], settings["smtp_port"], timeout=10)

        # Wenn TLS gewünscht (z.B. Port 587) -> STARTTLS
        if settings["use_tls"]:
            server.starttls()

        # Falls SMTP-Login nötig
        if settings["smtp_user"] and settings["smtp_pass"]:
            server.login(settings["smtp_user"], settings["smtp_pass"])

        server.send_message(msg)
        server.quit()

        logging.info(f"E-Mail verschickt: Betreff='{subject}' an {settings['to_email']}")

    except Exception as e:
        logging.error(f"Fehler beim E-Mail-Versand: {str(e)}")


########################################
# 6) Neue Route: Einstellungen (API + Mail) + Test-E-Mail
########################################
@app.route("/settings", methods=["GET", "POST"])
def settings():
    """
    Gemeinsame Seite für:
      1) API-Key-Einstellungen
      2) E-Mail-Einstellungen
      3) Test-E-Mail-Versand
    """
    if request.method == "POST":
        action = request.form.get("action")
        with get_connection() as conn:
            c = conn.cursor()

            # 1) API-Key speichern
            if action == "save_api":
                new_key = request.form.get("api_key", "").strip()
                new_secret = request.form.get("api_secret", "").strip()
                c.execute("DELETE FROM credentials")  # Nur 1 Datensatz halten
                c.execute("INSERT INTO credentials (api_key, api_secret) VALUES (?, ?)",
                          (new_key, new_secret))
                conn.commit()
                flash("API-Credentials wurden gespeichert.")
                logging.info("API-Credentials gespeichert/aktualisiert.")

            # 2) API-Key löschen
            elif action == "delete_api":
                c.execute("DELETE FROM credentials")
                conn.commit()
                flash("API-Credentials wurden gelöscht.")
                logging.info("API-Credentials gelöscht.")

            # 3) E-Mail-Einstellungen speichern
            elif action == "save_email":
                smtp_server = request.form.get("smtp_server", "").strip()
                smtp_port   = request.form.get("smtp_port", "587").strip()
                smtp_user   = request.form.get("smtp_user", "").strip()
                smtp_pass   = request.form.get("smtp_pass", "").strip()
                from_email  = request.form.get("from_email", "").strip()
                to_email    = request.form.get("to_email", "").strip()

                send_on_success = 1 if request.form.get("send_on_success") == "on" else 0
                send_on_error   = 1 if request.form.get("send_on_error") == "on" else 0
                use_tls         = 1 if request.form.get("use_tls") == "on" else 0

                # Alten Eintrag löschen, nur 1 Satz wird vorgehalten
                c.execute("DELETE FROM email_settings")
                c.execute("""
                    INSERT INTO email_settings (
                        smtp_server, smtp_port, smtp_user, smtp_pass,
                        from_email, to_email,
                        send_on_success, send_on_error, use_tls
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    smtp_server, smtp_port, smtp_user, smtp_pass,
                    from_email, to_email,
                    send_on_success, send_on_error, use_tls
                ))
                conn.commit()

                flash("E-Mail-Einstellungen wurden aktualisiert.")
                logging.info("E-Mail-Einstellungen wurden aktualisiert.")

            # 4) Test-E-Mail versenden
            elif action == "test_email":
                # Wir schicken eine Test-E-Mail
                subject = "Test-E-Mail von Bitmaster"
                body = (
                    "Hallo,\n\n"
                    "dies ist eine Test-E-Mail vom Bitmaster Tool.\n"
                    "Wenn du diese Mail siehst, funktioniert dein SMTP-Setup!\n"
                )
                send_email(subject, body)
                flash("Test-E-Mail wurde verschickt (siehe Logs für Details).")

        return redirect(url_for("settings"))

    # GET: Aktuelle Werte laden
    with get_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT api_key, api_secret FROM credentials ORDER BY id DESC LIMIT 1")
        api_row = c.fetchone()

    if api_row:
        saved_api_key, saved_api_secret = api_row
        mask_key = saved_api_key[:5] + "..." if saved_api_key else ""
        mask_secret = saved_api_secret[:5] + "..." if saved_api_secret else ""
    else:
        mask_key = ""
        mask_secret = ""

    mail_settings = load_email_settings()

    html = """
    <html>
    <body>
      <h1>Einstellungen</h1>
      <hr>

      <h2>API-Key-Einstellungen</h2>
      <form method="POST">
        <p>Aktueller API-Key (maskiert): {{ mask_key }}</p>
        <p>Aktueller Secret (maskiert): {{ mask_secret }}</p>
        <label>API Key:</label>
        <input type="text" name="api_key" value=""><br><br>
        <label>API Secret:</label>
        <input type="text" name="api_secret" value=""><br><br>

        <button type="submit" name="action" value="save_api">Speichern</button>
        <button type="submit" name="action" value="delete_api"
                onclick="return confirm('API-Keys wirklich löschen?');">
          Löschen
        </button>
      </form>
      <hr>

      <h2>E-Mail-Einstellungen</h2>
      <form method="POST">
        SMTP-Server:<br>
        <input type="text" name="smtp_server"
               value="{{ mail_settings.smtp_server if mail_settings else 'smtp.gmail.com' }}"><br><br>

        SMTP-Port (587 für STARTTLS, 465 für SSL):<br>
        <input type="number" name="smtp_port"
               value="{{ mail_settings.smtp_port if mail_settings else '587' }}"><br><br>

        SMTP-User:<br>
        <input type="text" name="smtp_user"
               value="{{ mail_settings.smtp_user if mail_settings else '' }}"><br><br>

        SMTP-Passwort:<br>
        <input type="password" name="smtp_pass" value="">
        <small>(Leer lassen, wenn nicht ändern)</small><br><br>

        Absender (From):<br>
        <input type="text" name="from_email"
               value="{{ mail_settings.from_email if mail_settings else '' }}"><br><br>

        Empfänger (To):<br>
        <input type="text" name="to_email"
               value="{{ mail_settings.to_email if mail_settings else '' }}"><br><br>

        <label>
          <input type="checkbox" name="send_on_success"
                 {% if mail_settings and mail_settings.send_on_success %}checked{% endif %}>
          E-Mail bei erfolgreichem Trade
        </label><br>

        <label>
          <input type="checkbox" name="send_on_error"
                 {% if mail_settings and mail_settings.send_on_error %}checked{% endif %}>
          E-Mail bei Fehler
        </label><br>

        <label>
          <input type="checkbox" name="use_tls"
                 {% if mail_settings and mail_settings.use_tls %}checked{% endif %}>
          STARTTLS aktivieren
        </label><br><br>

        <button type="submit" name="action" value="save_email">Speichern</button>
        <button type="submit" name="action" value="test_email">Test-E-Mail senden</button>
      </form>
      <hr>

      <p><a href="{{ url_for('index') }}">Zurück zum Hauptmenü</a></p>
    </body>
    </html>
    """
    return render_template_string(html,
        mask_key=mask_key, mask_secret=mask_secret,
        mail_settings=mail_settings
    )


########################################
# 7) Bitvavo-Client (optional) + Mock-Order
########################################
def get_bitvavo_client():
    with get_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT api_key, api_secret FROM credentials ORDER BY id DESC LIMIT 1")
        row = c.fetchone()

    if not row:
        raise Exception("Keine API-Credentials hinterlegt. Bitte in den Einstellungen hinzufügen.")

    api_key, api_secret = row
    return Bitvavo({
        'APIKEY': api_key,
        'APISECRET': api_secret,
        'RESTURL': 'https://api.bitvavo.com/v2',
        'WSURL': 'wss://ws.bitvavo.com/v2/',
        'ACCESSWINDOW': 30000
    })


def place_mock_order(asset, amount_eur):
    """
    Simuliert einen Kauf und gibt eine Fake-Response zurück.
    """
    logging.info(f"SIMULATION: Würde jetzt {amount_eur} EUR in {asset} investieren.")
    fake_price = 25000.0
    fake_filled_amount = float(amount_eur) / fake_price

    return {
        "orderId": "SIM-ORDER-12345",
        "fills": [
            {
                "amount": str(fake_filled_amount),
                "price": str(fake_price)
            }
        ]
    }


########################################
# 8) RETRY-Logik für Bitvavo-Aufrufe
########################################
def bitvavo_request_with_retry(func, *args, max_retries=3, **kwargs):
    attempt = 0
    while attempt < max_retries:
        try:
            return func(*args, **kwargs)
        except Exception as e:
            logging.warning(f"Bitvavo-Aufruf fehlgeschlagen (Versuch {attempt+1}/{max_retries}): {str(e)}")
            attempt += 1
            time.sleep(2)
    raise Exception(f"Bitvavo-Aufruf fehlgeschlagen nach {max_retries} Versuchen")


########################################
# 9) Scheduler-Logik
########################################
def load_schedules_into_scheduler():
    schedule.clear()
    # Täglicher Job um 00:00 Uhr -> update_prices_for_assets
    schedule.every().day.at("00:00").do(update_prices_for_assets)

    with get_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT id, weekday, time_of_day FROM schedules ORDER BY id")
        schedules_rows = c.fetchall()

    weekday_mapping = {
        "Monday": schedule.every().monday,
        "Tuesday": schedule.every().tuesday,
        "Wednesday": schedule.every().wednesday,
        "Thursday": schedule.every().thursday,
        "Friday": schedule.every().friday,
        "Saturday": schedule.every().saturday,
        "Sunday": schedule.every().sunday
    }

    for (sched_id, wd, tod) in schedules_rows:
        if wd not in weekday_mapping:
            logging.warning(f"Ungültiger Wochentag in DB: {wd}")
            continue

        def job_func(schedule_id=sched_id):
            execute_investment(schedule_id)

        weekday_mapping[wd].at(tod).do(job_func).tag(f"schedule_{sched_id}")


def execute_investment(schedule_id):
    """
    Führt für schedule_id alle definierten Käufe durch.
    """
    with get_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT asset, amount_eur FROM schedule_lines WHERE schedule_id = ?", (schedule_id,))
        lines = c.fetchall()

    if not lines:
        logging.info(f"Schedule {schedule_id} hat keine lines definiert.")
        return

    email_config = load_email_settings()

    # Bitvavo-Client nur laden, wenn wir nicht simulieren
    if not SIMULATION_MODE:
        try:
            bv = get_bitvavo_client()
        except Exception as e:
            logging.error(f"execute_investment: Kein Bitvavo-Client verfügbar: {str(e)}")
            # E-Mail bei Fehler?
            if email_config and email_config["send_on_error"]:
                subject = f"Fehler bei Schedule {schedule_id}"
                body = f"Konnte keinen Bitvavo-Client erstellen: {str(e)}"
                send_email(subject, body)
            return

    for (asset, amount_eur) in lines:
        try:
            market_symbol = f"{asset.upper()}-EUR"

            # Aktuellen Kurs abrufen
            if SIMULATION_MODE:
                current_price = 25000.0
            else:
                ticker = bitvavo_request_with_retry(bv.tickerPrice, {"market": market_symbol})
                current_price = float(ticker.get("price", 0.0))

            estimated_coins = float(amount_eur) / current_price if current_price else 0.0
            logging.info(
                f"Starte Kauf: {amount_eur} EUR => {asset} (Schedule {schedule_id}), "
                f"Kurs ~ {current_price:.2f} EUR, erwartet ~ {estimated_coins:.6f} {asset}"
            )

            # Order platzieren
            if SIMULATION_MODE:
                response = place_mock_order(asset, amount_eur)
            else:
                order_body = {"amountQuote": str(amount_eur)}
                response = bitvavo_request_with_retry(
                    bv.placeOrder, market_symbol, "buy", "market", order_body
                )

            # Erfolg?
            if "orderId" in response:
                filled_asset = 0.0
                total_cost = 0.0
                if "fills" in response:
                    for f in response["fills"]:
                        amt = float(f["amount"])
                        prc = float(f["price"])
                        filled_asset += amt
                        total_cost += amt * prc
                avg_price = total_cost / filled_asset if filled_asset else 0.0

                # In Datenbank speichern
                with get_connection() as conn2:
                    c2 = conn2.cursor()
                    c2.execute("""
                        INSERT INTO trades (
                            timestamp, asset, amount_eur,
                            filled_asset, avg_price, order_id
                        ) VALUES (?, ?, ?, ?, ?, ?)
                    """, (
                        datetime.datetime.now(),
                        asset.upper(),
                        amount_eur,
                        filled_asset,
                        avg_price,
                        response["orderId"]
                    ))
                    conn2.commit()

                logging.info(
                    f"Kauf erfolgreich (Schedule {schedule_id}): "
                    f"{filled_asset:.6f} {asset} @ ~{avg_price:.4f} EUR. "
                    f"OrderId={response['orderId']}"
                )

                # E-Mail bei Erfolg
                if email_config and email_config["send_on_success"]:
                    subject = f"Erfolgreicher Kauf: {asset}"
                    body = (
                        f"Schedule-ID: {schedule_id}\n"
                        f"Asset: {asset}\n"
                        f"EUR: {amount_eur}\n"
                        f"Erhaltene Menge: {filled_asset:.6f}\n"
                        f"Durchschnittspreis: {avg_price:.4f}\n"
                        f"OrderId: {response['orderId']}\n"
                        f"Zeitpunkt: {datetime.datetime.now()}\n"
                    )
                    send_email(subject, body)

            else:
                logging.error(f"Order fehlgeschlagen: {response}")
                if email_config and email_config["send_on_error"]:
                    subject = f"Fehler beim Kauf: {asset}"
                    body = f"Die Order ist fehlgeschlagen: {str(response)}"
                    send_email(subject, body)

        except Exception as e:
            logging.error(f"Fehler beim Kauf von {asset}: {str(e)}")

            # E-Mail bei Exception
            if email_config and email_config["send_on_error"]:
                subject = f"Exception beim Kauf: {asset}"
                body = (
                    f"Schedule-ID: {schedule_id}\n"
                    f"Asset: {asset}\n"
                    f"EUR: {amount_eur}\n"
                    f"Fehlermeldung: {str(e)}\n"
                )
                send_email(subject, body)


def run_scheduler():
    while True:
        schedule.run_pending()
        time.sleep(1)

threading.Thread(target=run_scheduler, daemon=True).start()


########################################
# 10) Historische Preise aktualisieren
########################################
def update_prices_for_assets():
    """
    Sammelt alle Assets aus schedule_lines und trades,
    ruft den aktuellen Preis ab und speichert ihn in historical_rates.
    """
    logging.info("Starte update_prices_for_assets() ...")
    if SIMULATION_MODE:
        logging.info("SIMULATION_MODE aktiv: Keine echten Preisupdates.")
        return

    with get_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT DISTINCT asset FROM schedule_lines")
        assets_lines = [r[0] for r in c.fetchall()]

        c.execute("SELECT DISTINCT asset FROM trades")
        assets_trades = [r[0] for r in c.fetchall()]

        all_assets = set(assets_lines + assets_trades)
        date_str = datetime.datetime.now().strftime('%Y-%m-%d')

    try:
        bv = get_bitvavo_client()
    except Exception as e:
        logging.error(f"Konnte Bitvavo-Client nicht erstellen (update_prices_for_assets): {str(e)}")
        return

    with get_connection() as conn:
        c = conn.cursor()
        for asset in all_assets:
            if not asset:
                continue
            symbol = f"{asset.upper()}-EUR"
            try:
                ticker = bitvavo_request_with_retry(bv.tickerPrice, {"market": symbol})
                price_eur = float(ticker.get("price", 0.0))

                c.execute("""
                    INSERT INTO historical_rates (date, asset, price_eur)
                    VALUES (?, ?, ?)
                """, (date_str, asset.upper(), price_eur))

                logging.info(f"Preis gespeichert: {asset} = {price_eur} EUR am {date_str}")
            except Exception as e2:
                logging.warning(f"Preis für {asset} konnte nicht geholt werden: {str(e2)}")

        conn.commit()


########################################
# 11) Routen: Startseite & Co.
########################################
@app.route("/")
def index():
    with get_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT id, weekday, time_of_day FROM schedules ORDER BY id")
        scheds = c.fetchall()

        schedules_list = []
        for (sid, wd, tod) in scheds:
            c.execute("SELECT asset, amount_eur FROM schedule_lines WHERE schedule_id=?", (sid,))
            lines_ = c.fetchall()
            schedules_list.append((sid, wd, tod, lines_))

    html = """
    <html>
    <body>
    <h1>Bitvavo Invest Tool</h1>

    {% with msgs = get_flashed_messages() %}
    {% if msgs %}
      <ul style="color:green">
      {% for m in msgs %}
        <li>{{ m }}</li>
      {% endfor %}
      </ul>
    {% endif %}
    {% endwith %}

    <p>
      <a href="{{ url_for('add_schedule') }}">Neuen Zeitplan anlegen</a> |
      <a href="{{ url_for('manual_balance') }}">Kontostand abrufen</a> |
      <a href="{{ url_for('trades_list') }}">Trades anzeigen</a> |
      <a href="{{ url_for('settings') }}">Einstellungen</a>
    </p>

    <h2>Aktuelle Zeitpläne</h2>
    {% if schedules_list %}
    <table border="1" cellpadding="4">
      <tr><th>ID</th><th>Wochentag</th><th>Uhrzeit</th><th>Assets</th><th>Aktionen</th></tr>
      {% for (sid, wd, tod, lines) in schedules_list %}
      <tr>
        <td>{{sid}}</td>
        <td>{{wd}}</td>
        <td>{{tod}}</td>
        <td>
          {% for (ast, amt) in lines %}
            {{ast}}: {{amt}} EUR<br>
          {% endfor %}
        </td>
        <td>
          <a href="{{ url_for('edit_schedule', schedule_id=sid) }}">Bearbeiten</a> |
          <a href="{{ url_for('delete_schedule', schedule_id=sid) }}"
             onclick="return confirm('Wirklich löschen?');">Löschen</a>
        </td>
      </tr>
      {% endfor %}
    </table>
    {% else %}
      <p>Keine Zeitpläne angelegt</p>
    {% endif %}

    </body>
    </html>
    """
    return render_template_string(html, schedules_list=schedules_list)


@app.route("/add_schedule", methods=["GET", "POST"])
def add_schedule():
    if request.method == "POST":
        wd = request.form.get("weekday")
        tod = request.form.get("time_of_day")

        assets = request.form.getlist("asset")
        amounts = request.form.getlist("amount_eur")

        with get_connection() as conn:
            c = conn.cursor()
            c.execute("INSERT INTO schedules (weekday, time_of_day) VALUES (?, ?)", (wd, tod))
            schedule_id = c.lastrowid

            for (ast, amt_str) in zip(assets, amounts):
                ast = ast.strip().upper()
                amt_val = 0.0
                try:
                    amt_val = float(amt_str)
                except:
                    pass
                if ast and amt_val > 0:
                    c.execute("""
                      INSERT INTO schedule_lines (schedule_id, asset, amount_eur)
                      VALUES (?, ?, ?)
                    """, (schedule_id, ast, amt_val))

            conn.commit()

        load_schedules_into_scheduler()
        logging.info(f"Neuer Zeitplan {schedule_id} angelegt: {wd} {tod}")
        flash("Neuer Zeitplan angelegt.")
        return redirect(url_for("index"))

    now_plus_2 = datetime.datetime.now() + timedelta(minutes=2)
    default_day = now_plus_2.strftime("%A")     # z.B. "Monday"
    default_time = now_plus_2.strftime("%H:%M") # z.B. "23:59"

    html = """
    <html>
    <body>
      <h1>Neuen Zeitplan anlegen</h1>
      <form method="POST">
        <label>Wochentag:</label>
        <select name="weekday">
          {% for day in ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"] %}
            <option value="{{ day }}" {% if day == default_day %}selected{% endif %}>{{ day }}</option>
          {% endfor %}
        </select><br><br>

        <label>Uhrzeit (HH:MM):</label>
        <input type="text" name="time_of_day" value="{{ default_time }}"><br><br>

        <hr>
        <p>Bis zu 3 Orders definieren:</p>
        <table>
          <tr><th>Asset</th><th>EUR</th></tr>
          {% for i in range(3) %}
          <tr>
            <td>
              <select name="asset">
                <option value="">-- bitte wählen --</option>
                {% for coin in allowed_assets %}
                  <option value="{{ coin }}">{{ coin }}</option>
                {% endfor %}
              </select>
            </td>
            <td><input type="number" step="0.01" name="amount_eur"></td>
          </tr>
          {% endfor %}
        </table>
        <br>
        <button type="submit">Speichern</button>
      </form>
      <hr>
      <p><a href="{{ url_for('index') }}">Zurück</a></p>
    </body>
    </html>
    """
    return render_template_string(
        html,
        allowed_assets=ALLOWED_ASSETS,
        default_day=default_day,
        default_time=default_time
    )


@app.route("/edit_schedule/<int:schedule_id>", methods=["GET", "POST"])
def edit_schedule(schedule_id):
    if request.method == "POST":
        wd = request.form.get("weekday")
        tod = request.form.get("time_of_day")

        assets = request.form.getlist("asset")
        amounts = request.form.getlist("amount_eur")

        with get_connection() as conn:
            c = conn.cursor()
            c.execute("UPDATE schedules SET weekday=?, time_of_day=? WHERE id=?", (wd, tod, schedule_id))
            c.execute("DELETE FROM schedule_lines WHERE schedule_id=?", (schedule_id,))

            for (ast, amt_str) in zip(assets, amounts):
                ast = ast.strip().upper()
                amt_val = 0.0
                try:
                    amt_val = float(amt_str)
                except:
                    pass
                if ast and amt_val > 0:
                    c.execute("""
                      INSERT INTO schedule_lines (schedule_id, asset, amount_eur)
                      VALUES (?, ?, ?)
                    """, (schedule_id, ast, amt_val))
            conn.commit()

        load_schedules_into_scheduler()
        logging.info(f"Zeitplan {schedule_id} aktualisiert: {wd} {tod}")
        flash(f"Zeitplan {schedule_id} wurde aktualisiert.")
        return redirect(url_for("index"))

    with get_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT weekday, time_of_day FROM schedules WHERE id=?", (schedule_id,))
        row = c.fetchone()
        if not row:
            flash(f"Zeitplan {schedule_id} existiert nicht.")
            return redirect(url_for("index"))
        wd, tod = row

        c.execute("SELECT asset, amount_eur FROM schedule_lines WHERE schedule_id=?", (schedule_id,))
        lines = c.fetchall()

    while len(lines) < 3:
        lines.append(("", 0.0))

    html = """
    <html>
    <body>
      <h1>Zeitplan {{ schedule_id }} bearbeiten</h1>
      <form method="POST">
        <label>Wochentag:</label>
        <select name="weekday">
          {% for day in ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"] %}
            <option value="{{ day }}" {% if day == wd %}selected{% endif %}>{{ day }}</option>
          {% endfor %}
        </select><br><br>

        <label>Uhrzeit (HH:MM):</label>
        <input type="text" name="time_of_day" value="{{ tod }}"><br><br>

        <hr>
        <p>Bis zu 3 Orders definieren:</p>
        <table>
          <tr><th>Asset</th><th>EUR</th></tr>
          {% for (ast, amt) in lines %}
          <tr>
            <td>
              <select name="asset">
                <option value="">-- bitte wählen --</option>
                {% for coin in allowed_assets %}
                  <option value="{{ coin }}" {% if coin == ast %}selected{% endif %}>{{ coin }}</option>
                {% endfor %}
              </select>
            </td>
            <td><input type="number" step="0.01" name="amount_eur" value="{{ amt }}"></td>
          </tr>
          {% endfor %}
        </table>
        <br>
        <button type="submit">Änderungen speichern</button>
      </form>
      <hr>
      <p><a href="{{ url_for('index') }}">Zurück</a></p>
    </body>
    </html>
    """
    return render_template_string(
        html,
        schedule_id=schedule_id,
        wd=wd,
        tod=tod,
        lines=lines,
        allowed_assets=ALLOWED_ASSETS
    )


@app.route("/delete_schedule/<int:schedule_id>")
def delete_schedule(schedule_id):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM schedule_lines WHERE schedule_id = ?", (schedule_id,))
        c.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))
        conn.commit()

    load_schedules_into_scheduler()
    flash(f"Zeitplan {schedule_id} gelöscht.")
    logging.info(f"Zeitplan {schedule_id} gelöscht.")
    return redirect(url_for("index"))


@app.route("/balance")
def manual_balance():
    if SIMULATION_MODE:
        flash("SIMULATION_MODE aktiv: Kein echter Kontostand.", "info")
        return redirect(url_for("index"))

    try:
        bv = get_bitvavo_client()
    except Exception as e:
        flash(f"Keine Credentials oder Fehler: {str(e)}", "error")
        logging.error(f"manual_balance: Kein Client. {str(e)}")
        return redirect(url_for("index"))

    # Letzter Snapshot
    with get_connection() as conn:
        c = conn.cursor()
        c.execute("""
          SELECT timestamp, currency, amount
          FROM balances
          ORDER BY id DESC LIMIT 50
        """)
        old_rows = c.fetchall()

        old_balance = {}
        last_ts = None
        if old_rows:
            last_ts = old_rows[0][0]
            old_balance_ts_rows = [r for r in old_rows if r[0] == last_ts]
            for (_, currency, amount) in old_balance_ts_rows:
                old_balance[currency] = amount

    # Aktueller Kontostand
    try:
        res = bitvavo_request_with_retry(bv.balance, {})
        if isinstance(res, dict) and "errorCode" in res:
            flash(f"Fehler: {res['errorCode']} - {res['error']}", "error")
            logging.error(f"Bitvavo balance error: {res}")
            return redirect(url_for("index"))

        now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        filtered = [b for b in res if float(b['available']) > 0]

        with get_connection() as conn:
            c = conn.cursor()
            for b in filtered:
                currency = b["symbol"]
                amount   = float(b["available"])
                c.execute("""
                  INSERT INTO balances (timestamp, currency, amount)
                  VALUES (?, ?, ?)
                """, (now_str, currency, amount))
            conn.commit()

        current_balance = {b["symbol"]: float(b["available"]) for b in filtered}

        flash(f"Kontostand abgerufen und gespeichert ({now_str}).")
        logging.info(f"Kontostand abgerufen: {len(filtered)} Einträge gespeichert.")

        html = """
        <html>
        <body>
          <h1>Kontostand</h1>
          <h2>Aktuell ({{ now_str }})</h2>
          {% if current_balance %}
            <ul>
              {% for sym, amt in current_balance.items() %}
                <li>{{ sym }}: {{ amt }}</li>
              {% endfor %}
            </ul>
          {% else %}
            <p>Kein Guthaben oder kein Zugriff.</p>
          {% endif %}

          <hr>
          <h2>Zuletzt abgerufener Kontostand{% if last_ts %} ({{ last_ts }}){% endif %}</h2>
          {% if old_balance %}
            <ul>
              {% for sym, amt in old_balance.items() %}
                <li>{{ sym }}: {{ amt }}</li>
              {% endfor %}
            </ul>
          {% else %}
            <p>Kein älterer Kontostand vorhanden.</p>
          {% endif %}

          <hr>
          <p><a href="{{ url_for('index') }}">Zurück</a></p>
        </body>
        </html>
        """
        return render_template_string(
            html,
            now_str=now_str,
            current_balance=current_balance,
            last_ts=last_ts,
            old_balance=old_balance
        )

    except Exception as e:
        flash(f"Fehler beim Kontostand: {e}", "error")
        logging.error(f"Fehler in manual_balance(): {str(e)}")
        return redirect(url_for("index"))


@app.route("/trades")
def trades_list():
    with get_connection() as conn:
        c = conn.cursor()
        c.execute("""
            SELECT timestamp, asset, amount_eur, filled_asset, avg_price, order_id
            FROM trades
            ORDER BY id DESC
        """)
        rows = c.fetchall()

    html = """
    <html>
    <body>
      <h1>Liste der Trades</h1>
      {% if rows %}
        <table border="1">
          <tr>
            <th>Datum</th>
            <th>Asset</th>
            <th>EUR</th>
            <th>Menge</th>
            <th>Preis</th>
            <th>OrderID</th>
          </tr>
          {% for (ts, ast, amt_eur, fill_amt, avg_pr, oid) in rows %}
          <tr>
            <td>{{ ts }}</td>
            <td>{{ ast }}</td>
            <td>{{ amt_eur }}</td>
            <td>{{ fill_amt }}</td>
            <td>{{ avg_pr }}</td>
            <td>{{ oid }}</td>
          </tr>
          {% endfor %}
        </table>
      {% else %}
        <p>Keine Trades in der DB</p>
      {% endif %}
      <hr>
      <p><a href="{{ url_for('index') }}">Zurück</a></p>
    </body>
    </html>
    """
    return render_template_string(html, rows=rows)


########################################
# MAIN
########################################
if __name__ == "__main__":
    init_db()
    load_schedules_into_scheduler()
    logging.info(f"Starte Flask-Server (SIMULATION_MODE={SIMULATION_MODE}) ...")
    # Debugmodus NICHT in Produktion verwenden
    app.run(host="0.0.0.0", port=5000, debug=True, use_reloader=False)
