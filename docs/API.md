# Exploration de l'espace personnel CFE

Observation du **11 novembre 2026**, avec le compte connecté par son titulaire.
Ce document contient des chemins et des structures, pas les données du compte.

Le MCP utilise désormais [l'API mobile](MOBILE_API.md) pour ses lectures principales et son
parcours de dépôt. Ce document décrit le portail, conservé pour l'historique des démarches.
Sa session est distincte de la connexion mobile renouvelable.

## Architecture et authentification

- Site personnel : `https://monespaceperso.cfe.fr/`.
- Framework observé : Microsoft Power Pages / Dynamics 365.
- Authentification : redirection vers `cfeb2cprod.b2clogin.com`, politique
  `B2C_1A_LogIn_Username`, identifiant/mot de passe puis vérification e-mail.
- Retour OIDC observé : `/signin-aad-b2c_1`.
- Cookie de session du portail : `.AspNet.ApplicationCookie`.
- Jeton CSRF : `GET /_layout/tokenhtml` renvoie un champ caché
  `__RequestVerificationToken`. Le transmettre comme en-tête du même nom en gardant les cookies.
- Le portail utilise aussi le cookie `__RequestVerificationToken` : le jeton et le cookie doivent
  provenir de la même session.

Le script public [cfe_common.js](https://monespaceperso.cfe.fr/cfe_common.js) implémente
`webapi.safeAjax` avec `shell.getTokenDeferred()`. Ce mécanisme correspond à la
[documentation Microsoft](https://learn.microsoft.com/en-us/power-pages/configure/web-api-http-requests-handle-errors).
Il n'est pas nécessaire de convertir le cookie de portail en jeton Bearer Dataverse.

## Consultation reproduite avec succès en HTTP

Les trois pages suivantes contiennent une grille `.entity-grid` :

| Page | Entité | Résultat vérifié |
| --- | --- | --- |
| `/mes-remboursements/` | `new_remboursement` | Réponse JSON avec les remboursements |
| `/mes-demarches/` | `incident` | Réponse JSON avec références, dates et statuts |
| `/mes-contrats/mes-beneficiaires/` | `new_lignedecontrat` | Réponse JSON ; une liste vide ne signifie pas que le titulaire est inéligible |

Lire les attributs `data-get-url`, `data-selected-view` et `data-view-layouts` dans chaque page.
Le dernier est du JSON encodé en base64. La vue active contient une
`Base64SecureConfiguration` **signée par le serveur**, à reprendre telle quelle.
Ne pas fabriquer ou modifier cette configuration : elle porte notamment les règles de visibilité.

La requête de lecture est un POST vers :

```text
/_services/entity-grid-data.json/{website_id}
```

Corps observé :

```json
{
  "base64SecureConfiguration": "<configuration reçue dans la page>",
  "sortExpression": "<tri reçu dans la vue>",
  "search": null,
  "page": 1,
  "pageSize": 10,
  "pagingCookie": "",
  "filter": null,
  "metaFilter": null,
  "nlSearchFilter": "",
  "timezoneOffset": -540,
  "customParameters": []
}
```

`timezoneOffset` est calculé depuis le fuseau local (exemple : Tokyo = -540).
Les filtres éventuels du portail sont distincts de la configuration signée.

Réponse : `Records`, `MoreRecords`, `ItemCount`, `PageCount`, `PageNumber`, `PageSize`,
`NextPagePagingCookie`. Les attributs d'un enregistrement exposent `Name`, `Value` et
`FormattedValue`. `Value` peut être un objet Dynamics `Money`, `OptionSetValue` ou `EntityReference`.
Le client retire les volumineuses métadonnées de schéma et conserve les valeurs et libellés.

## Parcours de remboursement : première étape observée

La page `/mes-remboursements/demande/` propose deux liens :

```text
/mes-remboursements/demande/depot/?form=fra
/mes-remboursements/demande/depot/?form=etr
```

Les trois étapes affichées sont :

1. Sélection d'un bénéficiaire.
2. Saisie d'une demande.
3. Dépôt des justificatifs.

La première étape est un **formulaire ASP.NET WebForms lié à l'entité `incident`**.
Elle ne correspond pas simplement à un POST JSON. Elle contient `__VIEWSTATE`,
`__EVENTVALIDATION`, `__EVENTTARGET` et des noms de champs préfixés par l'identifiant du contrôle.
Le bouton `NextButton` déclenche un postback. Le client devra reprendre les champs cachés de la
page courante, sans coder en dur leurs valeurs ni le préfixe du contrôle.

Pour un nouveau parcours observé, `EntityFormView_EntityID` était vide. La création et le statut du
dossier après le premier postback restent à vérifier ; il faut traiter ce postback comme une
opération pouvant créer un brouillon distant, et non comme une simple navigation.

| Champ | Rôle |
| --- | --- |
| `new_contactbeneficiaire` | GUID du bénéficiaire éligible |
| `new_typedemande` | `100000004` pour l'étranger, `100000005` pour la France |
| `new_paysdesoins` | GUID du pays tel que fourni dans les options |
| `new_circonstance` | Maladie `100000000`, tiers `100000001`, travail `100000002`, maternité `100000003` |
| `new_dateaccidentcauseauntiers` | Date conditionnelle pour un accident causé par un tiers |
| `new_dateaccidentdutravail` | Date conditionnelle pour un accident du travail |
| `new_datepresumeedaccouchement` | Date conditionnelle pour la maternité |

La page filtre les bénéficiaires à partir de `benefsFr_Etr` (valeurs `100000001` ou `100000003`).
Le MCP reproduit ce filtre. Il ne déduit pas l'éligibilité de la seule liste « gérer mes bénéficiaires ».
La page France impose le pays France ; la page étranger retire cette option.

Limites présentes dans les scripts du formulaire :

| Parcours | Soins | Justificatifs |
| --- | ---: | ---: |
| Étranger | 5 | 10 |
| France | 1 | 5 |

Le script commun mentionne PDF/JPG/JPEG/PNG et utilise un seuil `maxSize = 3000000` dans sa
vérification de format. **Ce seuil n'a pas été confirmé comme une limite d'envoi** ; il ne faut pas
en déduire une règle d'upload avant l'inspection de l'étape justificatifs.

## Autres routes présentes dans le code du site

Les routes de cette section ont été trouvées dans les scripts ; leur présence ne prouve pas que
toutes les opérations sont autorisées ni que leur contrat suffit à implémenter un dépôt.

| Route | Usage dans le code observé | État de validation |
| --- | --- | --- |
| `GET /_api/new_incident_new_soinset?fetchXml=...` | Liens entre incident et soins du dossier courant | Source seulement |
| `DELETE /_api/new_soins({id})` | Suppression des soins associés lors du retour à la première étape | Source seulement, jamais exécuté volontairement |
| `PATCH /_api/incidents({id})` | `{ "new_lecture": true }` lors de l'ouverture d'une démarche | Source seulement |
| `GET /_api/annotations({id})?$select=documentbody,filename,mimetype` | Téléchargement d'une pièce locale | Source seulement |
| `POST /_api/cloudflow/v1.0/trigger/f8d6eb29-02b8-ef11-b8e8-002248998b92` | Téléchargement via la GED | Source seulement |

La première étape exécute `deleteAllSoins()` si un ID de dossier existe déjà. **Revenir sur cette
étape dans un navigateur peut donc supprimer les lignes de soins du brouillon.** Une lecture HTTP
de la page ne lance pas ce JavaScript. Ne pas utiliser la navigation de retour comme une opération
de consultation d'un dossier existant.

Le détail d'une démarche est accessible via `/mes-demarches/detail/?id={incident_id}`.
Cette page a été lue par HTTP ; son script de marquage comme lu n'a pas été exécuté.

Pour la GED, le corps est `{ "eventData": "<objet JSON sérialisé>" }`. Le code du site utilise :

- Action `100000001`, `Entrée 1` = contact connecté, `Cible` = remboursement.
- Action `100000006`, `Cible` = document distant.

Le script décode une réponse JSON contenant `error` et `resultat`, puis un second JSON contenant
`filecontent` (base64) et `filename`. Ces téléchargements n'ont pas été testés.

## Messagerie et réexamen

La session du portail a été renouvelée par le titulaire le 11 novembre 2026. Deux lectures
authentifiées supplémentaires ont été observées :

- `/messagerie/message-recu/` : grille de messages reçus, même protocole de configuration
  signée et pagination que les autres tableaux. L'outil ne charge pas le détail des messages
  et n'exécute aucun script de marquage comme lu.
- `/messagerie/ecrire-message/` : première étape d'un formulaire WebForms. Les champs
  `new_theme` et `new_formulaireportaildelincident` donnent les thèmes et formulaires.

Le JavaScript de cette première étape associe le thème `100000000` (« Signaler une anomalie
sur un remboursement perçu : Remboursement de soins ») au formulaire `100000032`
(« Ecrire un message - Remboursement Soin »). Le parseur du MCP reprend les options et cette
association depuis la page courante ; une association absente ou modifiée produit une erreur.
Les valeurs cachées de session et de WebForms ne sont jamais retournées par cet outil.

Le bouton `NextButton` utilise `WebForm_DoPostBackWithOptions`. Un parcours réel autorisé a
ensuite confirmé les étapes suivantes :

1. `Suivant` crée un incident, dont l'identifiant apparaît dans `EntityFormView_EntityID`.
2. La seconde étape expose `description`, limitée à 2 500 caractères, et `#Subgrid_PJ`.
3. `#Subgrid_PJ a.create-action` ouvre une fenêtre avec `refentity=incident` et `refid`
   égal à cet identifiant. `AttachFile` puis `InsertButton` ajoutent la pièce. Chaque nom
   a été vérifié dans la grille avant de continuer.
4. Le bouton visible `.send-btn` déclenche le `NextButton` caché. La réponse conduit à
   `?success=true&type=messagerie` ; le script retire ensuite ces paramètres de l'adresse.
5. L'accusé visible est « Votre message a bien été pris en compte. ». Le même incident
   apparaît ensuite dans les démarches avec le statut « Envoyée à la CFE ».

L'envoi supervisé est journalisé dans le dossier privé. Le MCP expose les lectures, les
[brouillons de réexamen](REVIEWS.md), `cfe_submit_review` et la relecture distante avec
`cfe_get_review(refresh=true)`. Le nouvel adaptateur est testé avec un navigateur dont toutes
les requêtes sont interceptées ; le premier envoi réel avait utilisé le script supervisé.

La relecture vérifie l'identité et le même incident dans la grille des démarches, puis les
liens `a.file-link` de sa page de détail. Les annotations pointées par cette page sont lues
avec le CSRF du portail et comparées aux empreintes des copies nettoyées. Le PDF officiel
`WEB-CAS-*.pdf` doit aussi contenir le message approuvé dans son intégralité. Un simple statut
ou un indicateur JavaScript local ne suffit pas. Le `GET /_api/incidents(...)` sur les champs
du message a renvoyé 403 même avec la session du titulaire ; ce chemin n'est pas utilisé.

### Vérification des jetons mobiles sur le portail

Le schéma Dataverse `$metadata`, consulté avec l'authentification technique normale de
l'application, confirme les 16 actions `new_api_applimob_*` retrouvées dans l'APK. Aucune
action mobile de messagerie n'a été identifiée. Ce constat ne prouve pas qu'aucune autre API
n'existe.

Avec une session HTTP distincte et un jeton CSRF obtenu normalement, une lecture ciblée de
`/_api/incidents(<dossier du titulaire>)?$select=incidentid` a renvoyé HTTP 403, code
`90040120`, tant sans Bearer qu'avec le jeton adhérent CFE ou l'ID token mobile. Ces deux
jetons présentés en Bearer ne remplacent donc pas la session du portail dans le test effectué.
La page de composition redirigeait également vers la connexion (302). Les appels mobiles
authentifiés continuaient à fonctionner. Aucun jeton technique n'a été transmis au portail.

## Travail restant pour le dépôt web

Observer un parcours réel à partir de la première transition, en conservant le numéro du brouillon :

1. Postback initial et réponse : création, identifiant et statut exacts de l'`incident`.
2. Liste des types de soins et devises, validations de dates/montants, création et association des soins.
3. Upload : endpoint, encodage, taille effective, association au dossier et gestion des échecs partiels.
4. Validation finale : requête exacte, changement de statut, accusé de réception.
5. Reprise après timeout : distinguer un échec de transport d'un dépôt déjà reçu, sans réenvoyer aveuglément.

Les points 1 à 4 doivent être établis avant d'exposer un outil MCP de soumission. Le serveur actuel
n'expose aucun endpoint générique d'écriture ni d'outil simulant une soumission réussie.

## Vérification et captures

Les lectures HTTP authentifiées et les réponses des grilles ont été testées avec la session réelle.
Les tests automatisés portent sur l'isolation des cookies, le jeton CSRF frais, la configuration
signée, la pagination, la détection d'expiration, l'absence de renvoi automatique, les options et
le transport MCP stdio. Ils utilisent des données fictives.

La capture HAR brute se trouve hors dépôt, dans le dossier de données privé, et n'est pas un artefact
à partager. Le code versionnable ne contient ni cookie, ni jeton, ni identifiant personnel.
