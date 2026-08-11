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

## 4. Why Data Is Processed

Data is processed only to operate the bot's features, including to:

- verify members
- assign and manage roles
- send configured alerts
- store server settings
- run onboarding and welcome flows
- deliver ticketing, moderation, analytics, birthday, leveling, or utility features
- maintain security, stability, and abuse prevention

This fork is not intended to collect data for advertising or unrelated profiling.

## 5. No Sale of Personal Data

The operator of this fork does **not** intend to sell personal data gathered through normal bot operation.

This bot is currently operated as a **free, non-commercial tool**.

## 6. Third-Party Services

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

## 7. Retention

Data retention depends on feature type and configuration.

Examples based on the current codebase include:

- temporary OAuth state records are short-lived and expire quickly
- some temporary verification platform identity cache records are retained for up to **7 days**
- alert configuration remains stored until edited, disconnected, removed, or otherwise cleaned up
- guild configuration and feature data may remain stored while the bot is in use in that server

Because the bot is feature-driven, some operational records may persist until they are replaced, cleared, or no longer needed.

## 8. Security

Reasonable efforts may be made to protect stored bot data, but no system can be guaranteed perfectly secure.

Security depends in part on:

- the host environment
- database configuration
- protection of API keys and secrets
- server admin behavior
- third-party provider security

Users and server admins should avoid submitting unnecessary sensitive information through the bot.

## 9. Children and Sensitive Information

This bot is not intended to intentionally collect sensitive personal information.

Users should not submit highly sensitive information through tickets, AI prompts, alerts, forms, or other bot-driven workflows unless they fully understand the server's practices and the third-party services involved.

## 10. User and Server Choices

Because this is a Discord bot, many data practices are controlled by server administrators and bot operators.

Examples include:

- whether a feature is enabled
- what channels receive messages
- whether AI is enabled
- whether birthdays are collected
- whether social accounts are connected
- whether onboarding or verification data is saved

If you do not want a feature to process your data, do not use that feature and contact the relevant server staff or bot operator where appropriate.

## 11. Your Requests

Because this is a forked, self-hosted style deployment, data access, correction, deletion, and support requests should be directed to the operator of this fork rather than to upstream open-source authors.

- Operator name: `Blemondisberi`
- Contact email: `blemondisberi@gmail.com`
- Project URL: `https://github.com/blemondisberi-jpg/Logiq`

## 12. Changes to This Policy

This Privacy Policy may be updated as the bot changes, as integrations are added or removed, or as legal and operational requirements evolve.

Continued use of the bot after an updated policy is published constitutes acceptance of the updated policy.
