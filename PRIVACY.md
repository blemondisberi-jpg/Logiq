# Logiq Fork Privacy Policy

Last updated: August 11, 2026

## 1. Overview

This Privacy Policy applies to this forked and modified deployment of **Logiq**, a free and non-commercial Discord bot originally based on the open-source Logiq project.

This policy explains, in general terms, what information this fork may process in order to provide bot features inside Discord servers.

This policy is intended to reflect the current behavior of the repository and related deployments, but feature configuration may vary by server.

## 2. What This Fork Is

This fork is:

- a community-operated Discord bot deployment
- based on open-source software
- not currently offered as a paid commercial product
- intended for creator communities, server management, onboarding, alerts, and automation

Because this is a forked deployment, this policy applies to **this fork and its operator**, not to Discord itself or to any unrelated upstream service.

## 3. Data We May Process

Depending on which modules are enabled, the bot may process and store the following categories of data.

### Discord and Server Data

- Discord user IDs
- Discord guild (server) IDs
- Discord channel IDs
- Discord role IDs
- usernames, display names, or nicknames as needed for features
- configuration chosen by server administrators

### Verification and Role Data

- verification status
- configured verification roles
- selected self-assign roles
- platform-link verification choices
- temporary saved platform identity information used for onboarding flows

### Alert and Integration Data

- configured social alert usernames, channel targets, and alert templates
- linked account metadata needed for supported alert features
- API callback state values
- access tokens or refresh tokens for enabled integrations where required for the feature to work

Examples may include connected data for:

- YouTube
- TikTok
- Instagram
- Twitch
- Kick

### Utility and Community Feature Data

- birthday month and day if a user chooses to save a birthday
- ticket metadata and related support-channel state
- temporary voice ownership metadata
- server stats and channel automation settings
- audit logging and analytics event data
- leveling and economy progression data

### Optional AI Feature Data

If AI features are enabled, the bot may send message content or prompts to the configured AI provider in order to generate a response or summary.

## 4. Platform-Specific Data Notes

The bot does not collect the same information from every platform. What is processed depends on which integrations are configured.

### Discord

The bot may process:

- Discord user IDs
- guild IDs
- channel IDs
- role IDs
- usernames, display names, nicknames, and avatars as needed for bot features
- message metadata and interaction metadata needed to operate commands or automations
- server configuration selected by admins

The bot does **not** intentionally use Discord data for advertising, behavioral profiling, or sale to data brokers.

### Twitch

For Twitch alert or verification features, the bot may process:

- Twitch usernames and user IDs
- channel display data
- live status
- stream title, category, viewer count, thumbnail, and start time

The bot does **not** intentionally access Twitch chat history, whispers, payment information, watch history, or private creator analytics through normal operation of this fork.

### YouTube

For YouTube alert features, the bot may process:

- channel IDs, handles, display names, and thumbnails
- public live-stream and upload metadata
- video IDs, titles, descriptions, thumbnails, and publish/live timing data
- OAuth-linked account tokens or profile identifiers where the optional authenticated path is enabled

The bot does **not** intentionally access private watch history, payment data, or unrelated Google account content through normal operation of this fork.

### Kick

For Kick alert or verification features, the bot may process:

- Kick usernames, slugs, or account identifiers
- channel display data
- livestream status and stream metadata
- OAuth or webhook-related integration metadata where required

The bot does **not** intentionally access Kick payment data, private messages, or unrelated account history through normal operation of this fork.

### TikTok

For TikTok alert features, the bot may process:

- TikTok usernames or linked account identifiers
- profile display data returned by TikTok's APIs
- recent public or authorized post metadata used for configured alerts
- OAuth access or refresh tokens where TikTok connection is enabled

The bot does **not** intentionally access unrelated TikTok account data beyond what is needed for the enabled alert or connection flow.

### Instagram

For Instagram alert features, the bot may process:

- Instagram usernames
- professional account identifiers
- access tokens supplied for the feature
- public or accessible media metadata used for configured alerts

The bot does **not** intentionally access unrelated private Instagram account information beyond what is needed for the enabled alert or connection flow.

## 5. Why Data Is Processed

Data is processed only to operate the bot's features, including to:

- verify members
- assign and manage roles
- send configured alerts
- store server settings
- run onboarding and welcome flows
- deliver ticketing, moderation, analytics, birthday, leveling, or utility features
- maintain security, stability, and abuse prevention

This fork is not intended to collect data for advertising or unrelated profiling.

## 6. No Sale of Personal Data

The operator of this fork does **not** intend to sell personal data gathered through normal bot operation.

This bot is currently operated as a **free, non-commercial tool**.

## 7. Third-Party Services

Some bot functions rely on third-party infrastructure or APIs. Depending on configuration, data may be processed through:

- Discord
- MongoDB
- Railway or another host
- Twitch
- YouTube
- Kick
- TikTok
- Instagram
- OpenAI or Anthropic, if AI features are enabled

Each of those services has its own terms and privacy practices. Use of related bot features may involve data being sent to or received from those providers as part of normal operation.

### Relevant Platform Policy Links

Where applicable, users and server administrators should review the policies of the platforms they choose to connect or use through the bot:

- Discord Terms of Service: [https://discord.com/terms](https://discord.com/terms)
- Discord Privacy Policy: [https://discord.com/privacy-policy](https://discord.com/privacy-policy)
- Twitch Terms of Service: [https://legal.twitch.com/en/legal/terms-of-service/](https://legal.twitch.com/en/legal/terms-of-service/)
- Twitch Privacy Notice: [https://www.twitch.tv/p/en/legal/privacy-notice/](https://www.twitch.tv/p/en/legal/privacy-notice/)
- YouTube Terms of Service: [https://www.youtube.com/t/terms](https://www.youtube.com/t/terms)
- Google Privacy Policy: [https://policies.google.com/privacy](https://policies.google.com/privacy)
- YouTube API Services Terms of Service: [https://developers.google.com/youtube/terms/api-services-terms-of-service](https://developers.google.com/youtube/terms/api-services-terms-of-service)
- YouTube Developer Policies: [https://developers.google.com/youtube/terms/developer-policies](https://developers.google.com/youtube/terms/developer-policies)
- Kick Terms of Service: [https://kick.com/terms-of-service](https://kick.com/terms-of-service)
- Kick Privacy Policy: [https://kick.com/privacy-policy](https://kick.com/privacy-policy)
- TikTok Terms of Service: [https://www.tiktok.com/legal/page/us/terms-of-service/en](https://www.tiktok.com/legal/page/us/terms-of-service/en)
- TikTok Privacy Policy: [https://www.tiktok.com/legal/page/us/privacy-policy/en](https://www.tiktok.com/legal/page/us/privacy-policy/en)
- TikTok Developer Guidelines and Policies: [https://developers.tiktok.com/doc/our-guidelines-developer-guidelines/](https://developers.tiktok.com/doc/our-guidelines-developer-guidelines/)
- Instagram Terms of Use: [https://www.facebook.com/help/instagram/581066165581870](https://www.facebook.com/help/instagram/581066165581870)
- Meta Privacy Policy: [https://www.facebook.com/policy](https://www.facebook.com/policy)

## 8. Retention

Data retention depends on feature type and configuration.

Examples based on the current codebase include:

- temporary OAuth state records are short-lived and expire quickly
- some temporary verification platform identity cache records are retained for up to **7 days**
- alert configuration remains stored until edited, disconnected, removed, or otherwise cleaned up
- platform tokens or linked integration values may remain stored while the related feature stays connected
- guild configuration and feature data may remain stored while the bot is in use in that server
- birthday, leveling, ticket, role-menu, analytics, and other feature records may remain stored while the relevant feature or server data remains active

Because the bot is feature-driven, some operational records may persist until they are replaced, cleared, or no longer needed.

Removing the bot from a server generally stops future collection for that server, but it does not guarantee instant deletion of every historical operational record. Cleanup may occur through manual deletion, later overwrites, feature removal, or ordinary maintenance.

## 9. Security

Reasonable efforts may be made to protect stored bot data, but no system can be guaranteed perfectly secure.

Security depends in part on:

- the host environment
- database configuration
- protection of API keys and secrets
- server admin behavior
- third-party provider security

Users and server admins should avoid submitting unnecessary sensitive information through the bot.

## 10. Children and Sensitive Information

This bot is not intended to intentionally collect sensitive personal information.

Users should not submit highly sensitive information through tickets, AI prompts, alerts, forms, or other bot-driven workflows unless they fully understand the server's practices and the third-party services involved.

## 11. User and Server Choices

Because this is a Discord bot, many data practices are controlled by server administrators and bot operators.

Examples include:

- whether a feature is enabled
- what channels receive messages
- whether AI is enabled
- whether birthdays are collected
- whether social accounts are connected
- whether onboarding or verification data is saved

If you do not want a feature to process your data, do not use that feature and contact the relevant server staff or bot operator where appropriate.

## 12. Access, Correction, Disconnect, and Deletion Requests

Because this is a forked, self-hosted style deployment, data access, correction, deletion, and support requests should be directed to the operator of this fork rather than to upstream open-source authors.

Practical examples include:

- server administrators can remove the bot from a Discord server to stop future bot activity there
- server administrators can remove configured alerts, tickets, role menus, and other feature data from inside the bot
- linked social accounts can be disconnected through the bot's own disconnect commands where available
- users may contact the operator if they want stored information reviewed, corrected, or deleted where reasonably possible

Requests may be limited where the operator cannot reliably verify identity, where deletion would break an actively configured shared server feature, or where retention is still reasonably necessary for security, abuse prevention, debugging, or legitimate operational needs.

## 13. Contact

- Operator name: `Blemondisberi`
- Contact email: `blemondisberi@gmail.com`
- Project URL: `https://github.com/blemondisberi-jpg/Logiq`

## 14. Changes to This Policy

This Privacy Policy may be updated as the bot changes, as integrations are added or removed, or as legal and operational requirements evolve.

Continued use of the bot after an updated policy is published constitutes acceptance of the updated policy.
