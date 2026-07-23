FROM redmine:7
WORKDIR /usr/src/redmine

COPY ./configuration.yml config/configuration.yml

RUN apt-get update \
    && apt-get --yes install vim unzip \
    && bundle add 'rufus-scheduler' \
    \
    # Plug-in for Redmine (http://www.redmine.org/) to export project issues and versions as ICalendar (ICS) files
    # Latest version only runs on Redmine 5
    && git clone https://github.com/buschmais/redmics.git plugins/redmine_ics_export \
    && bundle install --without development test \
    \
    # Theme: https://github.com/mrliptontea/PurpleMine2
    && wget -O PurpleMine2.zip https://github.com/mrliptontea/PurpleMine2/archive/master.zip \
    && unzip PurpleMine2.zip -d public/themes \
    && mv public/themes/PurpleMine2-master public/themes/PurpleMine2 \
    && rm PurpleMine2.zip

# Other tested themes:
# - a1
# - alternate
# - classic
# - flatly_light_redmine
# - minelab
# - pixel-cookers
# - redmine_issue_readability_theme
# - redmine-theme-flat
# - redpenny

# https://www.redmine.org/projects/redmine/wiki/RedmineReceivingEmails#Schedule-email-receiving-with-Rufus-Scheduler
COPY ./check_email.rb config/initializers/check_email.rb
