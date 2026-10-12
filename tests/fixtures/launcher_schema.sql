-- Schema of launcher-v2.sqlite (Paradox Launcher v2, HOI4 1.19, knex migration 'updateDlcDependenciesId'),
-- read-only dump of the real database on 2026-10-12. Schema only, no data.
CREATE TABLE `knex_migrations` (`id` integer not null primary key autoincrement, `name` varchar(255), `batch` integer, `migration_time` datetime);
CREATE TABLE `knex_migrations_lock` (`index` integer not null primary key autoincrement, `is_locked` integer);
CREATE TABLE `mods` (`id` char(36) not null, `pdxId` varchar(255), `steamId` varchar(255), `gameRegistryId` text, `name` varchar(255), `displayName` varchar(255), "descriptionDeprecated" text, `thumbnailUrl` text, `thumbnailPath` text, `version` varchar(255), `tags` json default '[]', `requiredVersion` varchar(255), `arch` text, `os` text, `repositoryPath` text, `dirPath` text, `archivePath` text, `status` text not null, `source` text not null, `cause` text null default null, `timeUpdated` integer, `isNew` boolean, `createdDate` datetime, `subscribedDate` datetime, `size` integer, `metadataId` varchar(255), `remotePdxId` varchar(255), `remoteSteamId` varchar(255), `metadataVersion` varchar(255), `isMetadataApplied` boolean not null default '0', `metadataStatus` text check (`metadataStatus` in ('not_applied', 'missing', 'parsing_error', 'applied', 'validation_error')) not null default 'not_applied', `metadataGameId` varchar(255), `descriptionPdx` varchar(255), `descriptionSteam` varchar(255), `shortDescriptionPdx` varchar(255), `keepLatest` boolean not null default '1', `userVersion` boolean null default null, `remotePdxUserId` varchar(255) null, `remoteSteamUserId` varchar(255) null, `state` varchar(255) null, primary key (`id`));
CREATE TABLE `mods_dependencies` (`modId` char(36) not null, `modVersion` varchar(255) not null, `dependencyType` text not null, `dependencyId` char(36) not null, `dependencyVersion` varchar(255), `dependencyName` varchar(255), primary key (`modId`, `modVersion`, `dependencyId`));
CREATE TABLE `dlc` (`id` char(36) not null, `name` varchar(255) not null, `dirPath` varchar(255) not null, primary key (`id`));
CREATE TABLE playsets (
        id char(36) not null,
        name varchar(255) not null,
        isActive boolean,
        loadOrder varchar(255),
        pdxId int,
        pdxUserId char(36),
        createdOn datetime not null,
        updatedOn datetime,
        syncedOn datetime,
        "deprecatedLastServerChecksum" text,
        isRemoved boolean not null default false, `hasNotApprovedChanges` boolean not null default '0', `syncState` varchar(255), `state` text check (`state` in ('private', 'removedByUser', 'public', 'disabledByManager')) not null default 'private', `owned` boolean not null default '1', `author` varchar(255) not null default '', `subscribersCount` integer not null default '0', `ratingsCount` integer not null default '0', "coverImagePath" varchar(255), `description` varchar(255) default '', `offDisk` boolean not null default '0', `version` varchar(255) null, `lastSyncAttemptAt` datetime null, `coverImageUpdatedOn` datetime null, `lastUsedAt` datetime null,
        primary key (id),
        constraint uq_pdxId unique (pdxId)
      );
CREATE TABLE `playsets_mods` (`playsetId` char(36) not null, `modId` char(36) not null, `enabled` boolean default '1', `position` integer, foreign key(`playsetId`) references `playsets`(`id`) on delete CASCADE, foreign key(`modId`) references `mods`(`id`) on delete CASCADE);
CREATE TABLE `metadata_relationships` (`id` char(36) not null, `modMetadataId` varchar(255) not null, `relationshipId` varchar(255) not null, `relationshipType` text not null, `resourceType` text not null, `requiredVersion` varchar(255), `displayName` varchar(255), primary key (`id`));
CREATE TABLE `key_value_pairs` (`name` varchar(255) not null, `value` varchar(255) null, primary key (`name`));
CREATE TABLE `ugc` (`name` varchar(255) not null, `displayName` varchar(255) not null, `shortDescription` varchar(255) not null, primary key (`name`));
CREATE TABLE `transient_ownership` (`id` char(36) not null, `remoteId` varchar(255) not null, `endDate` datetime not null, `userId` char(36) not null, `username` varchar(255), primary key (`id`));
CREATE TABLE `playsets_dlcs` (`playsetId` char(36) not null, `dlcId` text not null, `enabled` boolean not null default '0', foreign key(`playsetId`) references `playsets`(`id`) on delete CASCADE, primary key (`playsetId`, `dlcId`));
CREATE UNIQUE INDEX `transient_ownership_remoteid_userid_unique` on `transient_ownership` (`remoteId`, `userId`);
CREATE INDEX `playsets_dlcs_playsetid_index` on `playsets_dlcs` (`playsetId`);
CREATE INDEX `playsets_dlcs_dlcid_index` on `playsets_dlcs` (`dlcId`);
