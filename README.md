# Pitriss Debian APT repository

Samostatný veřejný APT repozitář pro čtyři výslovně schválené projekty. Balíčky se **čtou pouze z GitHub Releases**, do zdrojových repozitářů se nic nezapisuje. Repozitář se publikuje přes **GitHub Pages / GitHub Actions**, nikoli commitem `.deb` do Git historie.

## Schválené projekty

| GitHub Releases | Balíčky | Distribuce |
|---|---|---|
| `Pitriss/kencoder` | `kencoder`, `encoder` | trixie |
| `Pitriss/pfx-split-qt` | `pfx-split` | trixie |
| `Pitriss/reddit2tg` | `reddit2tg` | trixie + Stretch varianty |
| `Pitriss/Jira-2-Teams` | `jira2teams` | trixie |

## Pravidla repozitáře

- Pro každou kombinaci **Package + architektura + distribuce + stable/testing** zůstávají **poslední dvě různé Debian verze** (porovnání `dpkg --compare-versions`). Opakovaně vydané stejné verze se neduplikují.
- `trixie`: stabilní releasy; `trixie-testing`: alpha/beta/rc/prerelease. Testovací kanál je opt-in.
- `stretch` a `stretch-testing`: jen výslovně označené soubory `*-stretch.deb` ze `reddit2tg`. Neoznačené balíčky nepovažujeme za kompatibilní se Stretchem.
- Architektury indexů: `amd64`, `arm64`, `armhf`; balíčky s architekturou `all` se indexují pro všechny podporované architektury.
- Automatický build každých 6 hodin a ruční `workflow_dispatch`, žádné upravování CI ve zdrojových projektech. Protože GitHub po 60 dnech neaktivity vypíná cron v public repo, centrální workflow nejvýše jednou za 40 dnů samo commitne malou značku aktivity do **centrálního** repozitáře (nikdy balíčky).
- Fail-closed: chyba API, stažení, nesoulad metadat balíčku, podpisu či indexů zastaví nasazení; dřívější úspěšně nasazený Pages artifact zůstane aktivní.
- Před importem se kontroluje skutečný `Package`, `Version`, `Architecture` z `.deb`, nikoli pouze jméno souboru.
- Publikace je výhradně veřejná. Soukromé balíčky nesmí být přidány.

## Jednorázové založení nového repozitáře

Po kontrole souborů spusť v kořeni tohoto nového projektu:

```bash
bash ./publish-github.sh
```

Požadavky na vlastní stanici: `gh`, `git`, `gpg`, `python3`, `dpkg`, přihlášení přes `gh auth login` jako **Pitriss**. Skript:

1. Inicializuje pouze tento nový lokální Git repozitář, vytvoří úvodní commit a veřejný `Pitriss/debian-repo` na GitHubu, provede push do `main`.
2. Přepne GitHub Pages na **GitHub Actions** pomocí API.
3. Vygeneruje (pouze napoprvé) RSA 3072 podpisový klíč v `~/.local/share/debian-repo/gnupg` s právy `0700`. Nikdy ho nepřidá do Git repozitáře. Bezheslový privátní klíč je nutné bezpečně zazálohovat a přiměřeně chránit; nedávej ho do žádného logu ani chatu.
4. Nahraje soukromý klíč přímo přes standardní vstup do šifrovaného GitHub Actions secretu `APT_REPO_GPG_PRIVATE_KEY`.
5. Spustí první workflow. Další aktualizace obstará harmonogram. GitHub Actions následně periodicky zapisují jen drobný heartbeat soubor, aby se cron po 60 dnech nevypnul.

**Před nasazením:** přesvědč se, že GitHub Actions má povolená práva pro Pages. Pokud API pro vytvoření Pages vrátí chybu, nastav ručně v **Settings → Pages → Build and deployment → GitHub Actions** a spusť `gh workflow run publish.yml -R Pitriss/debian-repo`.

Skript pouze čte veřejné GitHub Releases schválených projektů. Žádné zdrojové repozitáře neupravuje.

## Přidání APT zdroje na Debian 13

Nejprve musí existovat úspěšně nasazený veřejný repozitář na `https://pitriss.github.io/debian-repo/`.

```bash
sudo install -d -m 0755 /etc/apt/keyrings
curl -fsSL https://pitriss.github.io/debian-repo/repo-signing-key.asc | sudo tee /etc/apt/keyrings/pitriss-debian-repo.asc >/dev/null
sudo chmod 0644 /etc/apt/keyrings/pitriss-debian-repo.asc
printf '%s\n' 'deb [signed-by=/etc/apt/keyrings/pitriss-debian-repo.asc] https://pitriss.github.io/debian-repo trixie main' | sudo tee /etc/apt/sources.list.d/pitriss-debian-repo.list
sudo apt-get update
sudo apt-get install kencoder pfx-split jira2teams
```

Před důvěřováním klíči ověř jeho fingerprint proti výpisu z jednorázového publikačního skriptu (nebo proti bezpečně získanému fingerprintu od správce):

```bash
gpg --show-keys --with-fingerprint /etc/apt/keyrings/pitriss-debian-repo.asc
```

Pouhé stažení klíče ze stejné URL bez ověření fingerprintu chrání proti následným změnám repozitáře, ne proti prvotní kompromitaci zdroje. Po prvním ověření jej ulož do důvěryhodné dokumentace.

Pro testovací vydání lze přidat **další** řádek se suitou `trixie-testing`. Nedoporučuje se jej přidávat na produkční počítače, protože APT pak může preferovat novější prerelease balíčky. Balíčky `reddit2tg` určené pro Stretch jsou vedeny zvlášť a **nejsou zárukou kompatibility** s historickým systémem bez dodatečného testování.

## Lokální testy (bez síťového přístupu)

```bash
python3 -m unittest discover -v -s tests -p 'test*.py'
```

Testy vytvářejí umělé `.deb` balíčky a dočasný GPG klíč, testují výběr přesně dvou verzí, oddělení trixie/Stretch a stable/testing, verifikaci hashů a podpisů a skutečné `apt-get update` proti lokálnímu `file:` repozitáři. Vyžadují `dpkg-deb`, `dpkg`, `gpg` a `apt-get`.

## Obsah

- `config/projects.json`: jediný schvalovací seznam projektů a filtr balíčků.
- `scripts/build_repo.py`: stahování, kontrola `.deb`, retence, generování `Packages` a podepsaných `InRelease`.
- `scripts/verify_repo.py`: read-only kontrola před každým deploymentem.
- `.github/workflows/publish.yml`: plánovaný build a deployment Pages.
- `publish-github.sh`: jednorázové vytvoření veřejného GitHub projektu a inicializace GPG.

**Omezení:** Github Pages má limity velikosti a přenosů; build odmítá vygenerovat web přes 950 MB. Vlastní `.deb` soubory a privátní klíče se nikdy necommitují.
