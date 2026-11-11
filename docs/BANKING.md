# Changer le compte de versement des remboursements

Le parcours bancaire utilise le **portail web CFE**, avec sa session distincte de l'API mobile.
Il reproduit le formulaire officiel dans un navigateur isolé et transmet le justificatif demandé.
Il ne modifie pas le compte de prélèvement des cotisations : l'option correspondante reste à Non.

Les [essais d'accès avec la connexion mobile](BANKING_API_RESEARCH.md) ont confirmé l'existence
de ressources bancaires côté serveur, mais leurs lectures directes sont refusées à l'application.
Le jeton mobile seul n'ouvre pas non plus une session du portail. Aucun endpoint bancaire
utilisable avec cette seule connexion n'a été vérifié ; l'absence de tout endpoint privé n'est
pas démontrée.

## Outils MCP

1. Vérifier `cfe_portal_session_status`. Si nécessaire, utiliser `cfe_start_login`, saisir les
   identifiants et le code e-mail dans Chrome, puis `cfe_finish_login`.
2. Lire `cfe_bank_details` : coordonnées affichées par la CFE, pays proposés et démarches bancaires.
   Choisir le `country_id` dans la liste `countries`.
3. Appeler `cfe_prepare_bank_change(holder, iban, country_id, proof_path, bic="")`.
   Le justificatif doit être un fichier local absolu, PDF/JPEG/PNG, de **3 000 000 octets maximum**.
   La préparation crée uniquement un brouillon local avec une copie figée du fichier.
4. Relire le résultat ou utiliser `cfe_get_bank_change(draft_id)` : titulaire, pays, IBAN masqué,
   empreinte du justificatif et révision. `cfe_list_bank_changes` retrouve les brouillons du compte.
5. Pour un envoi demandé par le titulaire, appeler
   `cfe_submit_bank_change(draft_id, expected_revision)` avec cette révision.
6. `cfe_get_bank_change(draft_id, refresh=true)` consulte le statut du dossier connu sans le renvoyer.

Le BIC est contrôlé localement et transmis lorsque le formulaire du pays le demande.
Le pays et la longueur de l'IBAN sont aussi comparés à la configuration fournie par le serveur.
Le nom du titulaire suit les contraintes actuelles du portail : lettres sans accents et espaces,
70 caractères au maximum. Ne pas modifier automatiquement le nom fourni pour satisfaire cette règle.

## Bug des IBAN belges constaté le 11 novembre 2026

Le portail sépare le préfixe `BE` du reste de l'IBAN et ajoute des espaces dans le champ visible.
Pour un IBAN belge de 16 caractères, le champ restant contient 14 chiffres et 3 espaces, soit 17.
Le script du portail calcule pourtant `16 - 2 + floor(16 / 4) = 18` et exige cette longueur exacte.
Un IBAN valide échoue donc avant toute soumission au serveur.

Le MCP ajuste seulement cet attribut de longueur, selon :

```text
longueur du champ = longueur IBAN - 2 + floor((longueur IBAN - 1) / 4)
```

Il conserve la clé de contrôle modulo 97, le contrôle du pays, les validateurs du formulaire
et les validations du serveur. Il refuse une structure de formulaire ou une longueur attendue
qui ne correspond plus à l'ancien calcul ni au calcul corrigé.

## Résultats et reprise

- `draft` : préparation locale, aucune écriture bancaire distante.
- `sending` : journal d'une opération distante en cours. Après interruption, il devient `uncertain`.
- `submitted` : accusé du portail reçu, ou statut de la démarche connue confirmé par une lecture.
- `uncertain` : une écriture a commencé mais son résultat n'est pas suffisamment confirmé.
  Aucun renvoi automatique, même depuis un autre brouillon avec le même IBAN.

**`submitted` ne signifie pas que le nouvel IBAN est déjà actif.** Le portail annonce une prise
d'effet après validation et notification par la CFE. La lecture des coordonnées courantes reste
distincte du statut de la demande.

Le journal enregistre l'intention avant le premier postback, puis l'identifiant distant dès sa
réception. Un verrou entre processus empêche deux envois simultanés. Une empreinte liée au compte
et à l'IBAN bloque les doublons locaux, même avec un autre justificatif. Les demandes bancaires
déjà en cours dans l'historique du portail bloquent aussi un nouvel envoi.

Après une erreur, conserver le dossier privé et utiliser `refresh=true`. Si la démarche distante
ne confirme pas l'envoi, vérifier le portail ; ne pas supprimer le journal pour recommencer.
Les brouillons intermédiaires peuvent exister sur le portail dès le bouton Suivant.

## Vérification

Un changement réel avec un justificatif du titulaire a été envoyé depuis le portail après
correction du calcul de longueur. L'accusé et la démarche « Envoyée à la CFE » ont été vérifiés.
L'option d'utilisation du compte pour les cotisations était à Non.

L'implémentation MCP reproduit ce parcours. Les tests du navigateur interceptent toutes les
requêtes réseau : ils couvrent le formatage, le justificatif, la soumission, l'accusé et l'absence
de changement des prélèvements. Les autres tests couvrent les comptes, révisions, fichiers altérés,
concurrence, coupures, redémarrages, demandes en cours et doublons. Aucune seconde demande réelle
n'est envoyée pour tester le MCP.

Les données bancaires complètes, justificatifs et journaux restent dans le dossier privé
`~/.local/share/cfe-mcp/bank-changes`, hors du code. Les outils masquent l'IBAN des brouillons.
Un client MCP déjà lancé doit recharger le serveur pour découvrir les nouveaux outils.
