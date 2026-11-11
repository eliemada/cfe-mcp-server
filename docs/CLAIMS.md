# Demandes de remboursement

Le parcours est implémenté et testé avec un serveur simulé. Sa validation sur un dépôt réel
attend les soins et justificatifs fournis par le titulaire. Aucune donnée médicale ne doit
être déduite d'un exemple, d'un ancien remboursement ou de ce document.

## Données à fournir

`cfe_prepare_claim` prend :

| Champ | Source / règle |
| --- | --- |
| `beneficiary_id` | Identifiant renvoyé par `cfe_list_beneficiaries` |
| `country_code` | Pays des soins renvoyé par `cfe_claim_options` |
| `currency_code` | Devise dans laquelle les soins ont été facturés |
| `circumstance_code` | Valeur de circonstance du catalogue CFE |
| `circumstance_date` | Date de l'accident ou de la circonstance maternité, si applicable ; jamais la date du jour par défaut |
| `care_items` | Liste de soins : `code` actif et champs de `cfe_care_schema(code)` |
| `comment` | Commentaire fourni, facultatif |

Les dates utilisent `YYYY-MM-DD`. Les montants sont des chaînes décimales exactes, sans
séparateur de milliers. Un nombre flottant est refusé ; la virgule décimale est normalisée.
Le montant remboursé par un organisme local ne peut pas dépasser le montant facturé.
Les intervalles de séances ou de séjour doivent être ordonnés.

La devise peut différer de la monnaie usuelle du pays si la facture est ainsi libellée ; elle
doit exister dans le catalogue et être commune aux soins du dossier. `codeDevise` est déduit
de cette devise explicitement fournie. Les règles supplémentaires restent contrôlées par la CFE.

**`isPrescribed=true` signifie « absence de prescription »**, malgré le nom anglais trompeur
dans l'application. Cette case ne remplace la date de prescription que pour les formulaires
qui l'autorisent. Ne pas l'activer pour contourner une information manquante.

Les codes sont croisés avec le catalogue en ligne : 85 feuilles actives ont été observées.
Les 88 schémas embarqués ne sont donc pas tous exposés comme des soins utilisables.

## Pièces et limites

Les fichiers doivent être des PDF, JPEG ou PNG avec une signature cohérente et une taille
maximale de **3 145 728 octets**, limite trouvée dans l'application. Ce contrôle ne remplace
pas une lecture du document et ne vérifie pas qu'il constitue une facture recevable.

Types de document : `feuille`, `ordonnance`, `facture`, `autre`. Les nombres de pièces et de
soins sont relus dans `getParams` avant envoi : au 11 novembre 2026, étranger **10 pièces / 5 soins**,
France **5 pièces / 1 soin**. Le MCP exige au moins une pièce ; la CFE décide de leur suffisance.

L'ajout local crée une copie privée avec empreinte SHA-256. Une modification ultérieure du
fichier source ne change pas la copie. La copie elle-même est vérifiée avant chaque envoi.
Deux pièces identiques dans le même dossier sont refusées.

Le premier téléversement utilise une référence vide. La CFE fournit `ref_id` et `file_id` :
les pièces suivantes reprennent le même `ref_id`, transmis comme `careId` au dépôt final.
Un changement inattendu de référence bloque la suite. Les données distantes ne peuvent pas
être supprimées en fournissant un identifiant arbitraire : seul un fichier enregistré dans
ce brouillon et non encore soumis peut être retiré.

## Parcours MCP

1. `cfe_prepare_claim` crée le brouillon local.
2. `cfe_add_attachment` ajoute les copies locales des pièces fournies.
3. `cfe_validate_claim` vérifie le dossier et les pièces. Avec `server_check=true`, il appelle
   `checkCare` pour chaque soin réel fourni ; ce contrôle n'est pas une soumission.
4. `cfe_upload_claim_attachments` refait le contrôle CFE, puis transmet les pièces manquantes.
5. `cfe_get_claim` présente les données, pièces et révision courante.
6. `cfe_submit_claim(draft_id, expected_revision)` refait les contrôles et envoie le dossier.

Une correction complète est possible avec `cfe_update_claim` avant téléversement. Si des
pièces ont déjà été envoyées, les retirer avant de changer les données médicales. Les fichiers
retirés restent dans le stockage privé comme traces locales, mais ne figurent plus au dossier.

L'accusé brut est enregistré en privé. La réponse de l'outil indique `accepted_by_api` et le
chemin de l'accusé. L'état du remboursement relève du traitement ultérieur par la CFE.

## États, coupures et doublons

| État | Sens |
| --- | --- |
| `draft` | Brouillon modifiable ; certaines pièces peuvent déjà être téléversées |
| `ready` | Toutes les pièces actuelles sont téléversées |
| `uploading`, `deleting`, `submitting` | Intention enregistrée avant l'appel ; état temporaire |
| `submitted` | Canal d'erreur CFE explicitement vide ou `noError`, accusé enregistré |
| `uncertain` | Coupure, réponse ambiguë ou opération interrompue ; aucun renvoi autorisé automatiquement |

Un HTTP 200 sans accusé métier ne suffit pas. Les erreurs CFE sous forme d'objet ou de liste
sont vérifiées. Après interruption, une intention d'écriture inachevée devient `uncertain`.
Le verrou est partagé entre processus MCP et CLI ; il couvre les modifications et dépôts.

Un second appel de soumission sur un dossier accepté retourne son accusé local sans renvoyer.
Le journal compare aussi l'identité, la circonstance et les soins normalisés d'un nouveau
brouillon : changer le commentaire ou les pièces ne permet pas de répéter la même demande.
Les dossiers avec des groupes de soins différents et les dépôts effectués hors de ce MCP
nécessitent encore une vérification humaine des doublons.

En cas d'incertitude, vérifier les démarches dans le portail ou l'application. Ne pas effacer
le journal ni créer un autre brouillon pour retenter aveuglément. Il n'existe pas encore
d'API de rapprochement automatique vérifiée permettant de lever cet état.

## Ligne de commande

`cfe prepare /chemin/absolu/dossier.json` lit les mêmes champs que `cfe_prepare_claim`.
Le fichier JSON contient de vraies données personnelles et reçoit des permissions privées.
Les commandes suivantes utilisent l'identifiant local renvoyé :

```text
cfe claim <draft_id>
cfe attach <draft_id> /chemin/absolu/facture.pdf --type facture
cfe validate <draft_id>
cfe validate <draft_id> --server
cfe upload <draft_id>
cfe submit <draft_id> --revision <revision_courante>
```

Depuis ce projet, préfixer chaque commande par `uv run`.
