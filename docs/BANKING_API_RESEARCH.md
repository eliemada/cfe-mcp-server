# Accès bancaire avec la connexion mobile : essais du 11 novembre 2026

**Des ressources API bancaires existent sur le serveur. Les essais effectués ne permettent
pas d'y accéder avec la seule connexion mobile.** Cela ne démontre pas l'absence de tout endpoint
privé ou non documenté. Le changement bancaire actuellement implémenté utilise la session du portail.

## Méthode et périmètre

La session mobile a d'abord été vérifiée par une lecture des bénéficiaires réussie.
Les essais ont utilisé les jetons renouvelés du protocole réel de l'application :
jeton technique Dataverse et, pour les actions mobiles habituelles, jeton adhérent CFE.
Les identifiants de contact et de démarche utilisés provenaient du compte déjà authentifié.

Les seuls essais sur des données métier ont porté sur le contact du titulaire, sa demande
bancaire déjà envoyée et les liens bancaires filtrés sur son contact. Aucun POST de modification,
PATCH, DELETE, nouvel upload ou nouvelle demande bancaire n'a été effectué.

## Résultats observés

| Essai | Résultat |
| --- | --- |
| `GET /api/data/v9.2/$metadata` avec le jeton technique de l'application | HTTP 200 ; document CSDL de 27 691 964 octets |
| Lecture du contact du titulaire, champs IBAN partiels uniquement | HTTP 403, `0x80040220`, privilège `prvReadContact` absent |
| Lecture de la demande bancaire déjà envoyée, par son identifiant | HTTP 403, `0x80040220`, privilège `prvReadIncident` absent |
| Lecture de `new_domiciliationbancaires`, filtrée sur le contact du titulaire | HTTP 403, `0x80040220`, privilège `prvReadnew_Domiciliationbancaire` absent |
| Lecture des définitions Custom API dont le nom évoque un compte bancaire | HTTP 403, privilège `prvReadCustomAPI` absent |
| `GET /coordonnees-bancaires/` avec le seul ID token mobile, sans cookie web | HTTP 302 vers `/fr-FR/SignIn`, aucun cookie d'authentification portail |
| Connexion OIDC du portail avec `prompt=none` et l'ID token mobile comme `id_token_hint`, sans cookies navigateur | `interaction_required`, `AADB2C90077`, aucune réponse d'autorisation ni session portail |
| Action mobile `getParams` | Limites de soins et justificatifs uniquement ; aucune configuration bancaire trouvée |

Les refus de lecture ne sont donc pas simplement dus à un jeton mobile expiré : les actions
mobiles habituelles et la lecture des métadonnées fonctionnent avec la connexion testée.

## Ce que contient le catalogue serveur

Les 16 actions `new_api_applimob_*` publiées dans le CSDL correspondent aux 16 noms extraits
de l'application Android 3.7.0. Aucune de ces actions n'est dédiée à un changement d'IBAN.
Le sérialiseur mobile `sendRequest` observé construit une demande de soins ; sa présence ne
valide pas son utilisation pour une demande bancaire. Aucun corps bancaire expérimental n'y a été envoyé.

En revanche, le serveur décrit notamment :

- `contacts` avec des champs distincts pour l'IBAN des remboursements et celui des cotisations ;
- `incidents` avec les champs d'une demande bancaire ;
- `new_domiciliationbancaires` et `new_domiciliationbancairereferentiels` ;
- des flux internes comportant des paramètres bancaires, dont les effets et l'autorisation
  pour un usage adhérent ne sont pas établis. Ces flux n'ont pas été invoqués.

Le schéma d'un endpoint ne constitue pas une autorisation d'accès. Le rôle technique de
l'application refuse les lectures directes testées. Les appels mobiles spécialisés constituent
un chemin distinct avec leur propre vérification du jeton adhérent.

## Limites et conclusion pratique

La documentation Microsoft décrit le [catalogue CSDL et les opérations publiées](https://learn.microsoft.com/en-us/power-apps/developer/data-platform/webapi/web-api-service-documents).
Une [Custom API privée peut être omise de ce catalogue](https://learn.microsoft.com/en-us/power-apps/developer/data-platform/custom-api).
La lecture des définitions privées ayant été refusée, l'inventaire n'est pas déclaré exhaustif.

Le flux web utilise le même client OAuth public que l'application, mais les essais montrent
que le jeton mobile ne crée pas à lui seul une session portail. Microsoft décrit séparément
la [session SSO fondée sur les cookies du navigateur](https://learn.microsoft.com/en-us/azure/active-directory-b2c/session-behavior).

À ce stade, conserver le parcours du [guide bancaire](BANKING.md), avec authentification du
portail. Une future intégration bancaire fondée uniquement sur la connexion mobile nécessitera
un endpoint adhérent autorisé et un contrat de requête vérifié. Il ne faut ni annoncer que cet
endpoint existe et fonctionne, ni affirmer que toute API bancaire est absente.

Les réponses brutes, jetons éventuels contenus dans le diagnostic de connexion et identifiants
du titulaire restent dans `~/.local/share/cfe-mcp/research/bank-api/`, en fichiers privés hors dépôt.
