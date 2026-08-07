import api, { route, asApp } from '@forge/api';

// Per-install configuration, set on the app's admin page. Falls back to the
// environment variables the spike used, so an existing install keeps working.
const CONFIG_KEY = 'loupfeed-deployment';
const SECRET_KEY = 'loupfeed-shared-secret';

// `storage` is not a named export in @forge/api v8; it hangs off the default
// export. Wrong import made every trigger throw before it could forward.
const store = api.storage;

async function loadConfig() {
	let stored = {};
	try {
		stored = {
			url: await store?.get(CONFIG_KEY),
			secret: await store?.getSecret(SECRET_KEY),
		};
	} catch (err) {
		console.error(`CONFIG_READ_FAILED ${err?.message ?? err}`);
	}
	return {
		url: stored.url || process.env.DEPLOYMENT_URL,
		secret: stored.secret || process.env.SHARED_SECRET,
	};
}

/** Product triggers: forward what happened to the configured deployment. */
export async function run(event, context) {
	const eventType = event?.eventType || context?.moduleKey || 'unknown';
	const { url, secret } = await loadConfig();
	if (!url || !secret) {
		console.error('FORWARD_SKIPPED this installation is not configured yet');
		return;
	}
	try {
		const res = await api.fetch(`${url}/webhooks/atlassian`, {
			method: 'POST',
			headers: { 'Content-Type': 'application/json', 'X-Loupfeed-Secret': secret },
			body: JSON.stringify({ event, appAccountId: process.env.APP_ACCOUNT_ID }),
		});
		console.log(`FORWARDED ${eventType} -> ${res.status}`);
	} catch (err) {
		console.error(`FORWARD_FAILED ${eventType} ${err?.message ?? err}`);
	}
}

function escapeHtml(text) {
	return String(text ?? '')
		.replace(/&/g, '&amp;')
		.replace(/</g, '&lt;')
		.replace(/>/g, '&gt;');
}

function adf(text) {
	return {
		type: 'doc',
		version: 1,
		content: [{ type: 'paragraph', content: [{ type: 'text', text }] }],
	};
}

/**
 * Web trigger the deployment calls to post an agent reply. Replying from here
 * means the comment is authored by the APP, and the deployment needs no
 * Atlassian credential of its own.
 *
 * POST { issueKey?, pageId?, text, bodyAdf?, bodyStorage? }
 * with header X-Loupfeed-Secret
 *
 * The deployment renders the agent's markdown, because that belongs where it can
 * be tested; this app just forwards whatever it is given. `text` remains the
 * fallback for callers that send none, which is why replies used to arrive as
 * one flat paragraph with `**bold**` showing literally.
 */
export async function reply(request) {
	const { secret } = await loadConfig();
	const provided =
		request.headers?.['x-loupfeed-secret']?.[0] ?? request.headers?.['X-Loupfeed-Secret']?.[0];
	if (!secret || provided !== secret) {
		console.error('REPLY_REJECTED bad or missing secret');
		return { statusCode: 401, body: 'unauthorised' };
	}
	let payload;
	try {
		payload = JSON.parse(request.body || '{}');
	} catch {
		return { statusCode: 400, body: 'invalid json' };
	}
	const { issueKey, pageId, text, bodyAdf, bodyStorage } = payload;
	if ((!text && !bodyAdf && !bodyStorage) || (!issueKey && !pageId)) {
		return { statusCode: 400, body: 'need text and issueKey or pageId' };
	}
	try {
		const res = issueKey
			? await asApp().requestJira(route`/rest/api/3/issue/${issueKey}/comment`, {
					method: 'POST',
					headers: { 'Content-Type': 'application/json' },
					body: JSON.stringify({ body: bodyAdf ?? adf(text) }),
				})
			: await asApp().requestConfluence(route`/wiki/api/v2/footer-comments`, {
					method: 'POST',
					headers: { 'Content-Type': 'application/json' },
					body: JSON.stringify({
						pageId,
						body: {
							representation: 'storage',
							value: bodyStorage ?? `<p>${escapeHtml(text)}</p>`,
						},
					}),
				});
		console.log(`REPLIED ${issueKey ?? pageId} -> ${res.status}`);
		return { statusCode: res.ok ? 200 : 502, body: JSON.stringify({ status: res.status }) };
	} catch (err) {
		console.error(`REPLY_FAILED ${err?.message ?? err}`);
		return { statusCode: 502, body: 'reply failed' };
	}
}

// Paths the deployment may reach through this app. A proxy without an
// allowlist would hand anything holding the secret the app's full scope, so
// only the operations the agents actually need are permitted.
const ALLOWED = [
	{ method: 'GET', product: 'confluence', pattern: /^\/wiki\/api\/v2\/(pages|footer-comments)\/\d+(\?.*)?$/ },
	{ method: 'GET', product: 'confluence', pattern: /^\/wiki\/api\/v2\/spaces(\?.*)?$/ },
	{ method: 'GET', product: 'confluence', pattern: /^\/wiki\/rest\/api\/content\/search(\?.*)?$/ },
	{ method: 'POST', product: 'confluence', pattern: /^\/wiki\/api\/v2\/folders$/ },
	{ method: 'PUT', product: 'confluence', pattern: /^\/wiki\/rest\/api\/content\/\d+\/move\/append\/\d+$/ },
	{ method: 'GET', product: 'jira', pattern: /^\/rest\/api\/3\/issue\/[A-Z][A-Z0-9]*-\d+(\?.*)?$/ },
];

function permitted(method, product, path) {
	return ALLOWED.some(
		(rule) => rule.method === method && rule.product === product && rule.pattern.test(path)
	);
}

/**
 * Proxy the deployment's Atlassian reads/writes through the app, so the
 * deployment needs no Atlassian credential of its own.
 *
 * POST { product, method, path, body? } with header X-Loupfeed-Secret
 */
export async function proxy(request) {
	const { secret } = await loadConfig();
	const provided =
		request.headers?.['x-loupfeed-secret']?.[0] ?? request.headers?.['X-Loupfeed-Secret']?.[0];
	if (!secret || provided !== secret) {
		return { statusCode: 401, body: 'unauthorised' };
	}
	let payload;
	try {
		payload = JSON.parse(request.body || '{}');
	} catch {
		return { statusCode: 400, body: 'invalid json' };
	}
	const product = String(payload.product || '');
	const method = String(payload.method || 'GET').toUpperCase();
	const path = String(payload.path || '');
	if (!permitted(method, product, path)) {
		console.error(`PROXY_REFUSED ${method} ${product} ${path}`);
		return { statusCode: 403, body: 'not allowed' };
	}
	const options = { method, headers: { 'Content-Type': 'application/json' } };
	if (payload.body !== undefined) {
		options.body = JSON.stringify(payload.body);
	}
	try {
		const target = route([path]);
		const res =
			product === 'jira'
				? await asApp().requestJira(target, options)
				: await asApp().requestConfluence(target, options);
		const text = await res.text();
		console.log(`PROXIED ${method} ${path} -> ${res.status}`);
		return {
			statusCode: 200,
			headers: { 'Content-Type': ['application/json'] },
			body: JSON.stringify({ status: res.status, body: text }),
		};
	} catch (err) {
		console.error(`PROXY_FAILED ${method} ${path} ${err?.message ?? err}`);
		return { statusCode: 502, body: 'proxy failed' };
	}
}

// Confluence attachments need multipart, which the JSON proxy cannot carry,
// so bytes arrive base64-encoded and are re-assembled here. Forge caps web
// trigger payloads, so the deployment only routes small images this way and
// falls back to its own credential for large ones.
export async function attach(request) {
	const { secret } = await loadConfig();
	const provided =
		request.headers?.['x-loupfeed-secret']?.[0] ?? request.headers?.['X-Loupfeed-Secret']?.[0];
	if (!secret || provided !== secret) {
		return { statusCode: 401, body: 'unauthorised' };
	}
	let payload;
	try {
		payload = JSON.parse(request.body || '{}');
	} catch {
		return { statusCode: 400, body: 'invalid json' };
	}
	const { pageId, filename, contentType, dataBase64 } = payload;
	if (!pageId || !filename || !dataBase64) {
		return { statusCode: 400, body: 'need pageId, filename and dataBase64' };
	}
	try {
		const bytes = Buffer.from(dataBase64, 'base64');
		const form = new FormData();
		form.append('file', new Blob([bytes], { type: contentType || 'application/octet-stream' }), filename);
		const res = await asApp().requestConfluence(
			route`/wiki/rest/api/content/${pageId}/child/attachment`,
			{ method: 'POST', headers: { 'X-Atlassian-Token': 'no-check' }, body: form }
		);
		const text = await res.text();
		console.log(`ATTACHED ${filename} -> ${res.status}`);
		return {
			statusCode: res.ok ? 200 : 502,
			headers: { 'Content-Type': ['application/json'] },
			body: JSON.stringify({ status: res.status, body: text.slice(0, 2000) }),
		};
	} catch (err) {
		console.error(`ATTACH_FAILED ${filename} ${err?.message ?? err}`);
		return { statusCode: 502, body: 'attach failed' };
	}
}
