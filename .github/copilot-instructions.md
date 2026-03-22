# routing-generator — AI Dispatcher

**Non toccare `knowledge_base/psm_stack/`** senza backup esplicito.

## DISPATCHER (BOOTSTRAP)

### Prima risposta della sessione

1. Esegui `python .github/router.py --stats`
2. Mostra header:
```
🤖 [Modello] | Agente: [agente] | Priorità: [priority] | Routing: [stats]
```

### Router — modalità

| Modalità | Comando | Quando |
|----------|---------|--------|
| Diretto | `python .github/router.py --direct "query"` | Task semplici |
| Follow-up | `python .github/router.py --follow-up "query"` | Stessa sessione |
| Stats | `python .github/router.py --stats` | Inizio sessione |
| Audit | `python .github/router.py --audit` | Dopo modifiche routing-map |

**Ogni richiesta deve passare dal router.** Eccezione: coda documentale
dello stesso task (es. "aggiorna README dopo step completato").

### Postflight check (task non banali)
1. Router usato
2. Agente coerente con il task
3. Test verdi prima di dichiarare lo step completato
4. README tabella step aggiornata
5. **Se modificato `core/`** → propagare ai consumer: `rgen --update --target <path>` per ogni progetto in «Progetti consumer»

---

## PROGETTO

**routing-generator** — tool Python che genera automaticamente sistemi
di routing AI per qualsiasi progetto, partendo da pattern esistenti.

### Stack
- Python 3.12, pathlib, dataclasses, json, subprocess, re
- pytest + pytest-cov per i test
- Nessuna dipendenza esterna per il core (solo stdlib)

### Path workspace
| Path | Contenuto |
|------|-----------|
| `rgen/` | Package principale |
| `knowledge_base/` | Pattern disponibili |
| `core/` | File invarianti da copiare nei progetti target |
| `tests/` | Test pytest |

### Agenti disponibili
| Agente | Dominio |
|--------|---------|
| `developer` | Implementazione moduli rgen/ |
| `tester` | pytest, fixtures, coverage |
| `documentazione` | README, docstring, step tracking |
| `orchestratore` | Coordinamento, troubleshooting, architettura |

### Step di sviluppo
```
0 ✅  Scaffolding
1 ⏳  models.py + backup.py
2 ⏳  knowledge_base/psm_stack/ + PatternLoader
3 ⏳  questionnaire.py
4 ⏳  adapter.py
5+6 ⏳ writer.py + core files
7 ⏳  self_checker.py
8 ⏳  cli.py + integration test
```

### Vincoli critici
- **Test verde a ogni step** — nessuno step avanza senza test verdi
- **Backup sempre** — BackupEngine attivo prima di ogni write su disco
- **tmp_path nei test** — mai scrivere su disco reale nei test
- **Nessun {{VAR}} rimasto** — l'adapter verifica sempre la sostituzione completa

### Progetti consumer
Progetti che usano i `core/` files di questo repo e devono essere aggiornati
quando `core/` cambia (step 5 del Postflight):

| Progetto | Path locale | Layout | Comando update |
|----------|-------------|--------|----------------|
| ClaudeCodeTest | `H:\Projects\ClaudeCodeTest` | flat (root) | `rgen --update --flat --target "H:\Projects\ClaudeCodeTest"` |

---

## QUICK REFERENCE

```bash
# Sviluppo
pytest                          # tutti i test
pytest tests/test_backup.py -v  # step specifico
pytest --cov=rgen --cov-report=term-missing

# Router
python .github/router.py --stats
python .github/router.py --direct "implementa backup engine"
python .github/router.py --audit
```
