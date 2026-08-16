/**
 * API composable for MiniSky dashboard
 */

const API_BASE = '/api'

export function useApi() {

    async function request(path, options = {}) {
        const url = `${API_BASE}${path}`
        const response = await fetch(url, {
            headers: {
                'Content-Type': 'application/json',
                ...options.headers
            },
            ...options
        })

        if (!response.ok) {
            const error = await response.json().catch(() => ({ detail: response.statusText }))
            throw new Error(error.detail || 'Request failed')
        }

        return response.json()
    }

    // Health
    async function checkHealth() {
        return request('/health')
    }

    // Clusters
    async function listClusters() {
        return request('/v1/clusters')
    }

    async function getCluster(id) {
        return request(`/v1/clusters/${id}`)
    }

    async function createCluster(data) {
        return request('/v1/clusters', {
            method: 'POST',
            body: JSON.stringify(data)
        })
    }

    async function launchCluster(id) {
        return request(`/v1/clusters/${id}/launch`, { method: 'POST' })
    }

    async function stopCluster(id) {
        return request(`/v1/clusters/${id}/stop`, { method: 'POST' })
    }

    async function terminateCluster(id) {
        return request(`/v1/clusters/${id}`, { method: 'DELETE' })
    }

    // Jobs
    async function listJobs(clusterId = null) {
        const params = clusterId ? `?cluster_id=${clusterId}` : ''
        return request(`/v1/jobs${params}`)
    }

    async function getJob(id) {
        return request(`/v1/jobs/${id}`)
    }

    async function submitJob(data) {
        return request('/v1/jobs', {
            method: 'POST',
            body: JSON.stringify(data)
        })
    }

    async function cancelJob(id) {
        return request(`/v1/jobs/${id}/cancel`, { method: 'POST' })
    }

    return {
        checkHealth,
        listClusters,
        getCluster,
        createCluster,
        launchCluster,
        stopCluster,
        terminateCluster,
        listJobs,
        getJob,
        submitJob,
        cancelJob
    }
}
