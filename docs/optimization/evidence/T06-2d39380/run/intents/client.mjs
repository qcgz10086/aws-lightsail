import axios from 'axios';
import { invalidateQueryTags as invalidateCacheTags, queryCacheKey, getQueryCacheScope, queryGet, setQueryCacheScope, } from './queryCache.mjs';
const api = axios.create({
    baseURL: '/api',
    timeout: 120000,
    // R-5：登录态改走 HttpOnly Cookie，不再把 token 放 localStorage；
    // 同源请求自动带上 Cookie。
    withCredentials: true,
});
api.interceptors.request.use((config) => {
    config.queryCacheScope = getQueryCacheScope();
    return config;
});
api.interceptors.response.use((res) => {
    const method = res.config.method?.toLowerCase();
    if (method && method !== 'get' && method !== 'head' && method !== 'options') {
        const url = res.config.url || '';
        if (url.startsWith('/auth/'))
            invalidateCacheTags('auth');
        else if (url.startsWith('/credentials'))
            invalidateCacheTags('credentials', 'catalog', 'billing', 'instances', 'traffic');
        else if (url.startsWith('/v2/refresh')) {
            let body = res.config.data;
            if (typeof body === 'string') {
                try {
                    body = JSON.parse(body);
                }
                catch {
                    body = undefined;
                }
            }
            const kind = body && typeof body === 'object' ? body.kind : undefined;
            const params = { ...(body?.credential_id == null ? {} : { credential_id: body.credential_id }),
                ...(body?.region == null ? {} : { region: body.region }) };
            if (kind === 'billing')
                invalidateQueryScope('billing', params);
            else if (kind === 'instances')
                invalidateQueryScope('instances', params);
        }
        else if (url.startsWith('/instances')) {
            const match = url.match(/^\/instances\/([^/?]+)\/([^/?]+)/);
            const region = match?.[1];
            const name = match?.[2];
            const credentialId = res.config.params?.credential_id;
            if (region && name) {
                const decodedRegion = decodeURIComponent(region);
                const decodedName = decodeURIComponent(name);
                invalidateResource(decodedRegion, decodedName, credentialId);
                if (url.includes('/settings'))
                    invalidateQueryScope('traffic', { credential_id: credentialId, region: decodedRegion });
            }
            else
                invalidateCacheTags('instances', 'traffic');
        }
        else if (url.startsWith('/traffic')) {
            const credentialId = res.config.params?.credential_id;
            invalidateQueryScope('traffic', { credential_id: credentialId });
            invalidateQueryScope('instances', { credential_id: credentialId });
        }
        else if (url.includes('/billing/credits'))
            invalidateCacheTags('billing');
        else if (url.startsWith('/users'))
            invalidateCacheTags('users');
    }
    return res;
}, (error) => {
    if (error.response?.status === 401 && error.config?.queryCacheScope === getQueryCacheScope()
        && getQueryCacheScope() !== 'anonymous') {
        setQueryCacheScope('anonymous');
        const path = window.location.pathname;
        if (path !== '/login') {
            window.location.href = '/login';
        }
    }
    return Promise.reject(error);
});
export function getErrorMessage(err, fallback = '请求失败') {
    if (axios.isCancel(err) || (err instanceof DOMException && err.name === 'AbortError'))
        return '';
    if (axios.isAxiosError(err)) {
        const detail = err.response?.data?.detail;
        if (typeof detail === 'string')
            return detail;
        if (Array.isArray(detail))
            return detail.map((d) => d.msg).join('; ');
        return err.message || fallback;
    }
    if (err instanceof Error)
        return err.message;
    return fallback;
}
const READ_TTL = {
    '/auth/me': 30_000,
    '/credentials': 15_000,
    '/catalog/regions': 86_400_000,
    '/catalog/bundles': 86_400_000,
    '/catalog/blueprints': 86_400_000,
    '/v2/billing/summary': 15_000,
    '/v2/instances': 5_000,
    '/instances': 5_000,
    '/traffic/summary': 5_000,
    '/users': 15_000,
};
export function queryTtl(url) {
    return READ_TTL[url] ?? (url.startsWith('/metrics/') ? 15_000 : 0);
}
function scopeTag(domain, params = {}) {
    const credential = encodeURIComponent(String(params.credential_id ?? '*'));
    const region = encodeURIComponent(String(params.region ?? '*'));
    return `${domain}:scope:${credential}:${region}`;
}
function scopedTags(domain, params = {}) {
    return [domain, scopeTag(domain, params)];
}
function queryTagsFor(url, params = {}) {
    if (url.startsWith('/auth/'))
        return ['auth'];
    if (url.startsWith('/credentials'))
        return ['credentials', 'catalog', 'billing', 'instances', 'traffic'];
    if (url.startsWith('/catalog/'))
        return scopedTags('catalog', params);
    if (url.startsWith('/v2/billing/') || url.startsWith('/billing/'))
        return scopedTags('billing', params);
    if (url === '/v2/instances' || url === '/instances')
        return scopedTags('instances', params);
    const instanceMatch = url.match(/^\/(?:v2\/)?instances\/([^/?]+)\/([^/?]+)/);
    if (instanceMatch)
        return [
            ...scopedTags('instances', { ...params, region: instanceMatch[1] }),
            `instance:${params.credential_id ?? 'default'}:${instanceMatch[1]}:${instanceMatch[2]}`,
        ];
    if (url.startsWith('/traffic/'))
        return scopedTags('traffic', params);
    const metricMatch = url.match(/^\/metrics\/([^/?]+)\/([^/?]+)/);
    if (metricMatch)
        return [...scopedTags('metrics', { ...params, region: metricMatch[1] }),
            `metrics:${params.credential_id ?? 'default'}:${metricMatch[1]}:${metricMatch[2]}`];
    if (url.startsWith('/metrics/'))
        return ['metrics:any'];
    if (url.startsWith('/users'))
        return ['users'];
    return ['read-api'];
}
export function invalidateQueryFor(url, params = {}) {
    const domain = url.startsWith('/v2/billing/') || url.startsWith('/billing/') ? 'billing'
        : url.startsWith('/traffic/') ? 'traffic'
            : url === '/v2/instances' || url === '/instances' || url.startsWith('/instances/') ? 'instances'
                : url.startsWith('/catalog/') ? 'catalog' : null;
    if (domain)
        invalidateCacheTags(scopeTag(domain, params));
}
export function invalidateQueryScope(domain, params = {}) {
    invalidateCacheTags(scopeTag(domain, params));
}
export function invalidateResource(region, name, credentialId) {
    const params = { credential_id: credentialId, region };
    invalidateCacheTags(scopeTag('instances', params), scopeTag('traffic', params), scopeTag('metrics', params), `instance:${credentialId ?? 'default'}:${region}:${name}`, `metrics:${credentialId ?? 'default'}:${region}:${name}`);
}
export function readApi(url, params = {}, options = {}) {
    const key = queryCacheKey(url, params);
    return queryGet(key, async (signal) => {
        const response = await api.get(url, { params, signal });
        return response.data;
    }, { ...options, ttlMs: options.ttlMs ?? queryTtl(url),
        cacheIf: options.cacheIf ?? ((value) => !(value && typeof value === 'object' && 'refreshing' in value
            && Boolean(value.refreshing))),
        tags: [...new Set([...queryTagsFor(url, params), ...(options.tags ?? [])])] });
}
export function establishQueryCacheScope(scope) {
    setQueryCacheScope(scope == null ? 'anonymous' : String(scope));
}
export function invalidateQueryTags(...tags) {
    invalidateCacheTags(...tags);
}
export default api;
