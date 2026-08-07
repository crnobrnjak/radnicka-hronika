# Kačenje Radničke hronike na GitHub Pages + WordPress

Ova verzija je pripremljena tako da:

1. GitHub Actions jednom dnevno pokrene `main.py`.
2. Rezultat objavi kao GitHub Pages stranicu.
3. `state/seen_urls.json` sačuva nazad u repozitorijum, tako da se zna šta je zaista NOVO.
4. WordPress stranica na Narodnom dnevniku samo prikazuje tu GitHub Pages stranicu kroz iframe.

## A. Napravi GitHub repozitorijum

Na GitHubu napravi novi PUBLIC repository, na primer:

`radnicka-hronika`

Nemoj inicijalizovati README ako ćeš odmah uploadovati ovaj folder.

### Ako koristiš Git iz terminala

U ovom folderu:

```bash
git init
git add .
git commit -m "Initial Radnička hronika"
git branch -M main
git remote add origin https://github.com/TVOJ_USERNAME/radnicka-hronika.git
git push -u origin main
```

Ili možeš sve fajlove uploadovati kroz GitHub web interfejs.

## B. Uključi GitHub Pages

U repozitorijumu:

`Settings -> Pages`

Pod **Build and deployment / Source** izaberi:

`GitHub Actions`

Workflow je već u:

`.github/workflows/daily.yml`

## C. Prvi ručni test na GitHubu

Idi:

`Actions -> Radnička hronika -> Run workflow`

Sačekaj da workflow postane zelen.

GitHub Pages URL će biti približno:

`https://TVOJ_USERNAME.github.io/radnicka-hronika/`

Otvori ga u browseru.

## D. Dnevni raspored

Workflow trenutno radi svakog dana u:

**07:35 Europe/Belgrade**

U fajlu `.github/workflows/daily.yml`:

```yaml
schedule:
  - cron: "35 7 * * *"
    timezone: "Europe/Belgrade"
```

Ako želiš drugo vreme, promeni `35 7`.

GitHub scheduled workflow nije alarm u sekundu: može ponekad krenuti malo kasnije.
Zato je vreme namerno :35, a ne tačno na pun sat.

## E. WordPress / Narodni dnevnik

U WordPress adminu napravi stranicu:

**Radnička hronika**

Slug može biti:

`radnicka-hronika`

Dodaj **Custom HTML** blok i ubaci:

```html
<div style="width:100%;max-width:100%;">
  <iframe
    src="https://TVOJ_USERNAME.github.io/radnicka-hronika/"
    title="Radnička hronika"
    loading="lazy"
    style="width:100%;height:82vh;min-height:900px;border:0;background:#fafafa;"
  ></iframe>
</div>

<p style="font-size:0.9em;">
  Ako se prikaz ne učita,
  <a href="https://TVOJ_USERNAME.github.io/radnicka-hronika/" target="_blank" rel="noopener">
    otvori Radničku hroniku u novom prozoru
  </a>.
</p>
```

Zameni `TVOJ_USERNAME`.

Objavi stranicu.

Konačni URL će onda biti otprilike:

`https://narodnidnevnik.net/radnicka-hronika/`

## F. Kako sakriti pogrešan rezultat

Otvori u GitHub repozitorijumu:

`hidden_urls.txt`

Klikni olovku / Edit.

Dodaj puni URL članka, jedan po redu:

```text
https://neki-sajt.rs/clanak-koji-ne-zelim
```

Commit changes.

Pri sledećem dnevnom prolazu taj URL se neće prikazati.

Ako želiš odmah da nestane:

`Actions -> Radnička hronika -> Run workflow`

## G. Šta znači NOVO

GitHub workflow automatski čuva:

`state/seen_urls.json`

Ako neki URL nije bio u prethodnim prolazima, dobija oznaku **NOVO**.

Ne moraš ručno da "čistiš" listu svaki dan. Stari članci ostaju u trenutnom
vremenskom prozoru collectora, ali više nisu označeni kao NOVO.

## H. Ako GitHub ne može da upiše state

Ako korak **Sačuvaj stanje** prijavi permission grešku:

`Settings -> Actions -> General -> Workflow permissions`

proveri da workflow ima pravo pisanja. YAML već traži:

```yaml
permissions:
  contents: write
  pages: write
  id-token: write
```

Na ličnom javnom repozitorijumu to obično radi bez dodatnog podešavanja.

## I. GDELT 429

GDELT je pomoćni izvor, ne jedini izvor. Direktni RSS/HTML izvori nastavljaju
da rade čak i ako GDELT tog jutra vrati HTTP 429.

Jedno dnevno pokretanje dodatno smanjuje nepotrebno opterećenje GDELT API-ja.

## J. Koliko stare lokalne vesti prikazujemo

Dnevni workflow koristi:

```bash
--local-days 30
```

Ako RSS/HTML članak ima parsabilan datum i stariji je od 30 dana, odbacuje se.
Ako datum nedostaje, članak se ne odbacuje samo zbog toga.

GDELT i dalje koristi svoj `--timespan 14d`.
