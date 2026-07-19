# Grav CMS

Modern, Crazy Fast, Ridiculously Easy and Amazingly Powerful Flat-File CMS.

## Links

- **Official Website**: https://getgrav.org/
- **GitHub Repository**: https://github.com/getgrav/grav
- **Documentation**: https://learn.getgrav.org/
- **Docker Image**: https://hub.docker.com/r/linuxserver/grav

## Setup

Run the automated setup task:

```bash
invoke setup-grav
```

This will:

1. Create the data directory at `${VESSEL_DATA_DIR}/grav`
2. Start the Grav container
3. Wait for initialization
4. Install the following themes:
    - **Quark** - Default Grav theme
    - **Lingonberry** - Clean and minimal blog theme
    - **Future2021** - Modern and responsive theme
    - **Future** - Professional business theme
5. Install the **Instagram** plugin for embedding Instagram posts

## Access

- **Website**: http://localhost:8007
- **Admin Panel**: http://localhost:8007/admin

## First Time Setup

1. Open http://localhost:8007/admin
2. Create your admin account (first user becomes admin)
3. Configure your site settings
4. Choose a theme from the installed themes
5. Start creating content!

## Installed Themes

- **[Quark](https://github.com/getgrav/grav-theme-quark)** - The default Grav theme with modern design
- **[Lingonberry](https://github.com/getgrav/grav-theme-lingonberry)** - Clean and minimal blog theme
- **[Future2021](https://github.com/pmoreno-rodriguez/grav-theme-future2021)** - Modern responsive theme
- **[Future](https://github.com/absalomedia/grav-theme-future)** - Professional business theme

## Installed Plugins

- **Admin** - Admin panel (pre-installed with LinuxServer.io image)
- **Instagram** - Embed Instagram posts in your content

## Managing Themes and Plugins

```bash
# List available themes
docker exec -it -w /app/www/public grav bin/gpm index --themes-only

# List available plugins
docker exec -it -w /app/www/public grav bin/gpm index --plugins-only

# Install a new theme
docker exec -it -w /app/www/public grav bin/gpm install <theme-name> -y

# Install a new plugin
docker exec -it -w /app/www/public grav bin/gpm install <plugin-name> -y

# Update all themes and plugins
docker exec -it -w /app/www/public grav bin/gpm update -y

# Update Grav itself
docker exec -it -w /app/www/public grav bin/gpm selfupgrade -y
```

## Data Persistence

All Grav data is stored in `${VESSEL_DATA_DIR}/grav`:

- `/config/www` - Grav installation files
- `/config/www/user` - Your content, themes, plugins, and configuration

## Troubleshooting

### Container won't start

```bash
# Check logs
docker logs grav

# Restart container
docker restart grav
```

### Admin panel not accessible

The admin plugin should be pre-installed. If not:

```bash
docker exec -it -w /app/www/public grav bin/gpm install admin -y
```

### Clear cache

```bash
docker exec -it -w /app/www/public grav bin/grav clear-cache
```
