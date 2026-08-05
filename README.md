# Contact Hunter

**Contact Hunter** è un MVP proprietario per cercare, verificare, deduplicare e archiviare contatti B2B pubblicati sui siti ufficiali delle aziende.

Il progetto è pensato per sostituire le raccolte manuali: l'utente indica settore, paesi, città e dati richiesti; il sistema scopre i siti, analizza le pagine di contatto, salva email e telefoni con la relativa fonte e permette l'esportazione in Excel o CSV.

## Funzioni incluse

- dashboard web responsive;
- autenticazione amministratore tramite HTTP Basic;
- ricerca per settore, paese, città e parole chiave;
- scoperta automatica tramite **Brave Search API** opzionale;
- possibilità di fornire direttamente centinaia di URL iniziali;
- crawler asincrono con limiti per dominio;
- fallback Playwright per siti caricati tramite JavaScript;
- crawler Scrapy separato per lavorazioni batch;
- rispetto di `robots.txt` e crawl delay;
- blocco di localhost, IP privati, reti interne e domini in blacklist;
- estrazione di email, telefoni, WhatsApp, indirizzo e dati JSON-LD;
- riconoscimento di pagine contatti, sedi, note legali e impressum;
- deduplica persistente tramite fingerprint di dominio e contatto;
- classificazione dell'affidabilità della fonte;
- database SQLite immediato oppure PostgreSQL con Docker;
- importazione del censimento Excel già esistente;
- esportazione completa o per singola ricerca in XLSX e CSV;
- API JSON di base;
- test automatici e build Docker tramite GitHub Actions.

## Avvio immediato senza Docker

Richiede Python 3.12.

```bash
cp .env.example .env
python -m venv .venv
source .venv/bin/activate             # Windows: .venv\\Scripts\\activate
python -m pip install -e '.[dev]'
python -m playwright install chromium
uvicorn app.main:app --reload
```

Apri `http://127.0.0.1:8000`. Le credenziali iniziali sono quelle impostate in `.env`.

## Avvio con Docker

```bash
cp .env.example .env
# Modifica almeno ADMIN_PASSWORD, SECRET_KEY e POSTGRES_PASSWORD
docker compose up --build -d
```

L'applicazione sarà disponibile sulla porta `8000` e userà PostgreSQL.

## Importare il database esistente

Per proteggere i dati, il repository non contiene database reali. Copia il tuo file Excel nella cartella `data/imports` e importalo localmente.

```bash
python scripts/import_contacts.py data/imports/contatti.xlsx
```

In alternativa, dalla dashboard usa **Importa database esistente**. Il software individua l'intestazione del foglio, normalizza le email e ignora i record già presenti.

## Abilitare la ricerca automatica dei siti

1. Crea una chiave Brave Search API.
2. Inseriscila nel file `.env`:

```env
BRAVE_SEARCH_API_KEY=la-tua-chiave
```

Senza chiave il crawler funziona comunque, ma la ricerca deve contenere URL iniziali. La chiave non deve mai essere inserita nel codice o pubblicata nel repository.

## Esempio di ricerca

- Settore: `studi dentistici`
- Paesi: `Italia, Francia, Germania`
- Città: facoltative
- Parole chiave: `implantologia, ortodonzia`
- Dati: email, telefono, WhatsApp, indirizzo
- Numero massimo: `500`

Il motore genera query diverse, raggruppa i risultati per dominio, analizza un numero limitato di pagine per sito e conserva l'URL esatto della fonte.

## Crawler Scrapy batch

Per un elenco di siti già disponibile:

```bash
scrapy crawl contacts \
  -a start_urls=https://studio1.it,https://studio2.eu \
  -a country=Italy \
  -a keywords=implantologia,ortodonzia \
  -O risultati.json
```

## Pubblicazione tramite GitHub Actions

Il workflow `Test e build` esegue test, controllo statico e costruzione Docker a ogni push su `main`.

È incluso anche `Deploy manuale su VPS`, avviabile dalla scheda **Actions** dopo aver configurato questi secret nel repository:

- `VPS_HOST`
- `VPS_USER`
- `VPS_SSH_PRIVATE_KEY`
- `VPS_APP_PATH`

Sul server il repository deve essere già clonato nella cartella indicata da `VPS_APP_PATH` e Docker Compose deve essere disponibile. Il deploy è solo manuale (`workflow_dispatch`), quindi non parte accidentalmente a ogni commit.

## API

- `GET /health`
- `GET /api/searches/{job_id}`
- `GET /api/contacts?limit=100`
- `GET /exports/xlsx`
- `GET /exports/csv`

L'interfaccia OpenAPI di FastAPI è disponibile su `/docs` dopo l'autenticazione del browser.

## Sicurezza e conformità incorporate

- nessun tentativo di superare CAPTCHA, login o paywall;
- nessuna generazione o supposizione di email non pubblicate;
- `robots.txt` rispettato per impostazione predefinita;
- frequenza per dominio configurabile;
- risposte HTML limitate a 2 MB per impostazione predefinita;
- redirect nuovamente validati;
- blocco SSRF verso IP privati, loopback e reti locali;
- blacklist di social network e piattaforme non destinate alla scansione;
- fonte e data di verifica conservate per ogni record;
- avvertenza sull'uso commerciale e sulla base giuridica.

Il software aiuta a raccogliere dati aziendali pubblici, ma non determina automaticamente la liceità di una campagna commerciale. Prima dell'uso devono essere configurati finalità, base giuridica, informativa, lista di opposizione e tempi di conservazione adatti all'organizzazione.

## Limiti dell'MVP

L'esecuzione dei lavori avviene in un worker interno al processo FastAPI. È sufficiente per un singolo server e per volumi iniziali. Per un servizio con più istanze o ricerche molto grandi, il passo successivo è spostare i job su una coda persistente, per esempio Redis/RQ o Celery, e aggiungere proxy conformi, monitoraggio e gestione utenti avanzata.

## Struttura

```text
app/                 applicazione FastAPI, dashboard e servizi
scraper/             crawler Scrapy batch
scripts/             importazione e strumenti operativi
tests/               test automatici
data/imports/         cartella locale per file Excel privati (ignorati da Git)
.github/workflows/    CI e build Docker
```

## Proprietà

Il progetto non dipende da servizi di sviluppo proprietari: puoi conservarlo in un repository GitHub privato, eseguirlo sul tuo server e sostituire in futuro il provider di ricerca implementando un nuovo adapter in `app/services/search.py`.
