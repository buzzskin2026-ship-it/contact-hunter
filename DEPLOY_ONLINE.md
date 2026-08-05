# Avvio online senza terminale

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/buzzskin2026-ship-it/contact-hunter)

Questa procedura non richiede terminale, Docker o installazioni sul computer.

## Procedura

1. Premi il pulsante **Deploy to Render** qui sopra.
2. Crea un account Render oppure accedi.
3. Collega l'account GitHub `buzzskin2026-ship-it` quando Render lo richiede.
4. Nella schermata del Blueprint inserisci:
   - `ADMIN_PASSWORD`: una password amministratore nuova e sicura;
   - `BRAVE_SEARCH_API_KEY`: la chiave Brave Search API per consentire al software di trovare automaticamente i siti. Puoi aggiungerla anche in seguito dalle variabili del servizio.
5. Conferma con **Apply** o **Deploy Blueprint**.
6. Render creerà automaticamente:
   - il servizio web Contact Hunter;
   - il database PostgreSQL;
   - il collegamento privato fra applicazione e database.
7. Quando il deploy risulta `Live`, apri l'indirizzo `https://...onrender.com` mostrato nella dashboard.
8. Accedi con:
   - utente: `admin`;
   - password: quella inserita in `ADMIN_PASSWORD`.

## Prima ricerca

Dalla dashboard crea una ricerca, per esempio:

- settore: `studi dentistici`;
- paesi: `Italia, Francia, Germania`;
- parole chiave: `implantologia, ortodonzia`;
- limite: `100`.

Senza `BRAVE_SEARCH_API_KEY` l'applicazione funziona comunque sugli URL inseriti manualmente, ma non può scoprire autonomamente nuovi siti tramite il motore di ricerca.

## Aggiornamenti

Ogni modifica futura al branch `main` viene distribuita automaticamente su Render.

## Sicurezza

Non inserire password o chiavi API nei file del repository. Conservale esclusivamente nelle variabili d'ambiente di Render. Revoca il personal access token GitHub precedentemente condiviso nella chat.
