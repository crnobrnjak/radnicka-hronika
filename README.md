# Radnička hronika — V2

V2 je namerno stroža od V1. Cilj nije da svaki dan proizvede mnogo linkova,
nego da ono što prikaže uglavnom zaista bude radničko pitanje u Srbiji.

## Glavne promene

1. `sourcecountry:serbia` više se ne tretira kao dokaz da se događaj desio u Srbiji.
   GDELT upit sada traži i srpski geografski kontekst.
2. Samo jedan GDELT poziv po prolazu, što smanjuje HTTP 429.
3. Štrajk glađu se eksplicitno odbacuje.
4. Očigledne strane teme (npr. Grčka/Rumunija/Lufthansa) se odbacuju po naslovu.
5. "Radnik stradao" nije automatski povreda/smrt na radu. Potreban je kontekst
   radnog mesta ili eksplicitno "na radu".
6. "Otkaz" nije automatski otpuštanje radnika. Potreban je kontekst radnog odnosa.
7. Sekcijski URL više ne pada automatski na globalni `/feed/` portala.
   Ako sekcijski RSS ne postoji, koristi baš kategorijsku HTML stranicu.
8. Dodati su izrazi za akontacije, "nije isplaćena", "bez plate", rad na određeno,
   neprijavljene radnike itd.
9. `debug/rejected.jsonl` pokazuje šta je odbačeno i zašto.

## Pokretanje

Ako već imaš `.venv` iz V1, možeš napraviti novi ili koristiti postojeći.

```bash
python -m pip install -r requirements.txt
python main.py --self-test
python main.py --fresh
```

Otvori:

```text
out/report.html
```

Za brzi test samo direktnih izvora:

```bash
python main.py --fresh --no-gdelt
```

Samo jedan izvor:

```bash
python main.py --fresh --no-gdelt --only "Glas Šumadije"
```

## Šta da pošalješ ako nešto opet izgleda čudno

Najkorisniji su:

```text
out/report.html
debug/source_status.json
debug/rejected.jsonl
debug/raw_candidates.jsonl
```

`rejected.jsonl` je nov u V2: uz svaki odbačeni naslov piše i razlog.

## Važno

Pre prvog pravog pokretanja promeni u `main.py`:

```python
DEFAULT_UA = "RadnickaHronika/0.2 (+contact: promeni-me@example.com)"
```

i stavi stvarnu kontaktnu adresu.


## GitHub + WordPress

Za kompletna uputstva vidi `DEPLOY.md`.

## V4 dopune

- radnički štrajk glađu može da prođe ako istovremeno postoji jasan radnički akter i konkretan radni povod;
- poznati RSS feedovi se pokušavaju pre HTML stranica;
- GDELT upit je kraći i ima fallback/dijagnostiku;
- `wordpress-embed.html` uklanja dupli scrollbar pomoću `postMessage` automatskog resize-a.
