---
title: YouTube Growth Lab Privacy Policy
---

# Privacy Policy

YouTube Growth Lab is operated by Lenore AG for analysis of an authorized YouTube channel.

## Data and purpose

The application uses Google OAuth to read the channel owner's authorized YouTube data. It stores channel and video identifiers, titles, upload dates, cumulative statistics and available aggregate analytics, including watch time, retention, traffic sources, subscriber changes and, when separately authorized, revenue. These data support performance analysis, forecasts and evaluation of channel-owner content experiments.

The application does not request permission to publish, delete or modify YouTube videos or create engagement. It does not import individual viewer profiles or comment text as part of its analytics pipeline.

## Storage and access

The production application runs on Vercel and stores analytics and experiment records in Neon PostgreSQL. OAuth credentials and the refresh token are held in the deployment's environment variables, not in this public repository. Private dashboard data require the application's access token. GitHub Pages serves these public informational pages.

Analytics are retained for historical comparison until removed by the operator. V2 has no automatic expiry or self-service deletion endpoint. The authorized channel owner can request deletion through the operator's existing contact channel; deletion must be performed by the operator, including consideration of retained backups. Do not post tokens or private channel data in public GitHub issues.

## Control of authorization

The channel owner can revoke the application's Google access through [Google Account connections](https://myaccount.google.com/connections). Revocation prevents further authorized imports; it does not automatically delete records already stored by the application. Contact the operator separately for deletion of stored records.

## Sharing and use

The application uses authorized data for this channel's analytics and does not sell it or use it to generate fake engagement. Hosting and database providers process data as needed to run the application. YouTube API data use is subject to the applicable Google and YouTube API terms.

[Application home](./)
