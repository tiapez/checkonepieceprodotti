# Bot Telegram – nuovi prodotti

Il bot controlla periodicamente le pagine elencate in `elenco_negozi.txt` e invia un messaggio Telegram quando compare un prodotto mai visto prima.

## Configurazione

1. Apri Telegram, scrivi a **@BotFather**, usa `/newbot` e copia il token ricevuto.
2. Scrivi almeno un messaggio al nuovo bot.
3. Apri `https://api.telegram.org/bot<TOKEN>/getUpdates` sostituendo `<TOKEN>` e copia il valore `message.chat.id`.
4. Copia `.env.example` in `.env` e inserisci token e chat ID.
5. Installa e avvia:

```powershell
python -m pip install -r requirements.txt
python bot.py
```

Al primo controllo il bot memorizza i prodotti esistenti senza inviare notifiche. Nei controlli successivi notifica soltanto i nuovi prodotti. I dati sono conservati in `prodotti_visti.db`.

Per eseguire un solo controllo:

```powershell
python bot.py --once
```

Per vedere il numero di prodotti rilevati in ogni negozio, senza configurare Telegram e senza modificare la baseline:

```powershell
python bot.py --report
```

Per una prova senza messaggi Telegram, imposta `DRY_RUN=true` nel file `.env`.

## Avvio automatico su Windows

Apri **Utilità di pianificazione**, crea un'attività da eseguire all'accesso e indica:

- Programma: il percorso completo di `python.exe`
- Argomenti: `bot.py`
- Avvia in: la cartella di questo progetto

Il processo rimane attivo e ripete il controllo ogni `CHECK_INTERVAL_SECONDS`.

Quando un negozio risponde con 403/429, il bot prova automaticamente a leggerlo
con Microsoft Edge in modalità invisibile. Questa funzione può essere disattivata
impostando `BROWSER_FALLBACK=false` nel file `.env`.

## GitHub Actions

Il workflow `.github/workflows/product-monitor.yml` esegue un controllo ogni 10
minuti e può anche essere avviato manualmente dalla scheda **Actions**.

Nel repository GitHub apri **Settings → Secrets and variables → Actions** e crea
questi due repository secret:

- `TELEGRAM_BOT_TOKEN`: il token ricevuto da BotFather
- `TELEGRAM_CHAT_ID`: l'identificativo della chat Telegram

La baseline `prodotti_visti.db` viene conservata nella cache privata di GitHub
Actions e non viene aggiunta al repository. Alla prima esecuzione vengono
memorizzati i prodotti esistenti senza inviare notifiche.

### Iscrizioni pubbliche

Qualunque utente può aprire il bot su Telegram e usare:

- `/start` per iscriversi, ricevere un messaggio per negozio con i prodotti
  disponibili o prenotabili e le notifiche future;
- `/stop` per annullare l'iscrizione;
- `/help` per visualizzare i comandi disponibili.

Gli iscritti e l'ultimo comando elaborato sono salvati nello stesso database
conservato dalla cache di Actions. Poiché il workflow parte ogni 10 minuti, la
risposta ai comandi non è istantanea e può arrivare al controllo successivo.
