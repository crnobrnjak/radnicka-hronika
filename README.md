# Radnička hronika

Automatski pregled vesti o položaju radnika u Srbiji.

Program sakuplja članke iz odabranih domaćih medija preko RSS-a ili kategorijskih stranica, uz dopunski GDELT upit. Zatim determinističkim pravilima izdvaja teme kao što su:

- štrajkovi i radnički protesti;
- otkazi i gubitak radnih mesta;
- neisplaćene ili umanjene zarade;
- mobing i povrede radnih prava;
- loši i nebezbedni uslovi rada;
- neprijavljen rad;
- povrede i smrt na radu;
- sindikalna pitanja.

Filter normalizuje latinicu i ćirilicu i koristi osnove reči i kontekstualne regex obrasce, a ne samo doslovno poklapanje ključnih reči. Nema AI/ML klasifikacije.

## Lokalno pokretanje

```bash
python -m pip install -r requirements.txt
python main.py --self-test
python main.py --fresh
```

Bez GDELT-a:

```bash
python main.py --fresh --no-gdelt
```

## Glavni fajlovi

- `main.py` — sakupljanje i filtriranje
- `sources.json` — praćeni izvori
- `hidden_urls.txt` — ručno skriveni članci
- `debug/` — kandidati, odbačeni članci i status izvora
- `state/seen_urls.json` — već viđeni URL-ovi

GitHub Actions pokreće collector jednom dnevno i objavljuje rezultat preko GitHub Pages.
