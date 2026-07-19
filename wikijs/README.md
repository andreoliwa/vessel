# Setup

Using [Requarks/wiki: Wiki.js | A modern and powerful wiki app built on Node.js](https://github.com/Requarks/wiki).
Replacing [aviaryan/VSCodeNotebook: 📝 Use VS Code as a reliable note-taking/journal application](https://github.com/aviaryan/VSCodeNotebook).

1. Connect to the database container.
    ```bash
    vessel postgres connect
    ```
2. Create user/database:
    ```
    CREATE USER wikijs WITH PASSWORD '<password here>';
    CREATE DATABASE wikijs;
    GRANT ALL PRIVILEGES ON DATABASE wikijs to wikijs;
    ```
3. Disconnect and connect as the WikiJS user:
    ```
    vessel postgres connect --user wikijs --password-env WIKIJS_PASSWORD --database wikijs
    ```
4. Run this on the database:
    ```
    CREATE EXTENSION pg_trgm;
    ```
5. Start WikiJS:
    ```
    inv up-logs wikijs
    ```
6. Navigate to http://localhost:8004/ to complete the setup.
7. Configure [the site title and turn off comments](http://localhost:8004/a/general).
8. Configure [site tree navigation](http://localhost:8004/a/navigation).
9. Choose "Database - PostgreSQL" as the [search engine](http://localhost:8004/a/search).
10. Configure [Git storage](http://localhost:8004/a/storage).
    1. Follow the documentation: [Git | Wiki.js](https://docs.requarks.io/storage/git)
    2. SSH Private Key Mode: contents
    3. Copy the private key with `cat ~/.ssh/wikijs.pem | pbcopy`
    4. Paste it on the "SSH Private Key Contents" field.
    5. When the container is set up for the first time, run "Import Everything" at the bottom of this page.
