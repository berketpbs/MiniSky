<template>
  <div>
    <div class="flex justify-between items-center mb-8">
      <h1 class="text-3xl font-bold text-white">Clusters</h1>
      <button 
        @click="showCreateModal = true"
        class="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition"
      >
        + New Cluster
      </button>
    </div>
    
    <!-- Clusters Table -->
    <div class="bg-gray-800 rounded-lg border border-gray-700 overflow-hidden">
      <table class="w-full">
        <thead class="bg-gray-700">
          <tr>
            <th class="px-6 py-3 text-left text-xs font-medium text-gray-300 uppercase">Name</th>
            <th class="px-6 py-3 text-left text-xs font-medium text-gray-300 uppercase">Provider</th>
            <th class="px-6 py-3 text-left text-xs font-medium text-gray-300 uppercase">State</th>
            <th class="px-6 py-3 text-left text-xs font-medium text-gray-300 uppercase">Nodes</th>
            <th class="px-6 py-3 text-left text-xs font-medium text-gray-300 uppercase">IP</th>
            <th class="px-6 py-3 text-left text-xs font-medium text-gray-300 uppercase">Actions</th>
          </tr>
        </thead>
        <tbody class="divide-y divide-gray-700">
          <tr v-if="clusters.length === 0">
            <td colspan="6" class="px-6 py-8 text-center text-gray-400">
              No clusters. Create one to get started.
            </td>
          </tr>
          <tr v-for="cluster in clusters" :key="cluster.cluster_id" class="hover:bg-gray-750">
            <td class="px-6 py-4">
              <router-link :to="`/clusters/${cluster.cluster_id}`" class="text-blue-400 hover:underline">
                {{ cluster.name }}
              </router-link>
            </td>
            <td class="px-6 py-4 text-gray-300">{{ cluster.provider }}</td>
            <td class="px-6 py-4">
              <span :class="stateClass(cluster.state)" class="px-2 py-1 rounded text-xs font-medium">
                {{ cluster.state }}
              </span>
            </td>
            <td class="px-6 py-4 text-gray-300">{{ cluster.num_nodes }}</td>
            <td class="px-6 py-4 text-gray-300">{{ cluster.head_ip || '-' }}</td>
            <td class="px-6 py-4">
              <div class="flex space-x-2">
                <button 
                  v-if="cluster.state === 'init'"
                  @click="launch(cluster.cluster_id)"
                  class="px-3 py-1 bg-green-600 text-white rounded text-sm hover:bg-green-700"
                >
                  Launch
                </button>
                <button 
                  v-if="cluster.state === 'up'"
                  @click="stop(cluster.cluster_id)"
                  class="px-3 py-1 bg-yellow-600 text-white rounded text-sm hover:bg-yellow-700"
                >
                  Stop
                </button>
                <button 
                  v-if="cluster.state !== 'terminated'"
                  @click="terminate(cluster.cluster_id)"
                  class="px-3 py-1 bg-red-600 text-white rounded text-sm hover:bg-red-700"
                >
                  Terminate
                </button>
              </div>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    
    <!-- Create Modal -->
    <div v-if="showCreateModal" class="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
      <div class="bg-gray-800 rounded-lg p-6 w-full max-w-md border border-gray-700">
        <h2 class="text-xl font-bold text-white mb-4">Create Cluster</h2>
        <form @submit.prevent="create">
          <div class="mb-4">
            <label class="block text-sm font-medium text-gray-300 mb-2">Name</label>
            <input 
              v-model="newCluster.name"
              type="text"
              class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded text-white"
              required
            />
          </div>
          <div class="mb-4">
            <label class="block text-sm font-medium text-gray-300 mb-2">Provider</label>
            <select 
              v-model="newCluster.provider"
              class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded text-white"
            >
              <option value="mock">Mock (Testing)</option>
              <option value="aws">AWS</option>
              <option value="gcp">GCP</option>
              <option value="azure">Azure</option>
            </select>
          </div>
          <div class="mb-4">
            <label class="block text-sm font-medium text-gray-300 mb-2">Nodes</label>
            <input 
              v-model.number="newCluster.num_nodes"
              type="number"
              min="1"
              class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded text-white"
            />
          </div>
          <div class="flex justify-end space-x-3">
            <button 
              type="button"
              @click="showCreateModal = false"
              class="px-4 py-2 bg-gray-600 text-white rounded hover:bg-gray-700"
            >
              Cancel
            </button>
            <button 
              type="submit"
              class="px-4 py-2 bg-blue-600 text-white rounded hover:bg-blue-700"
            >
              Create
            </button>
          </div>
        </form>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { useApi } from '../composables/useApi'

const { listClusters, createCluster, launchCluster, stopCluster, terminateCluster } = useApi()

const clusters = ref([])
const showCreateModal = ref(false)
const newCluster = ref({
  name: '',
  provider: 'mock',
  num_nodes: 1
})

function stateClass(state) {
  const classes = {
    up: 'bg-green-500 text-white',
    launching: 'bg-yellow-500 text-black',
    stopped: 'bg-gray-500 text-white',
    terminated: 'bg-red-500 text-white',
    error: 'bg-red-600 text-white',
    init: 'bg-blue-500 text-white'
  }
  return classes[state] || 'bg-gray-500 text-white'
}

async function loadClusters() {
  try {
    clusters.value = await listClusters()
  } catch (e) {
    console.error('Failed to load clusters:', e)
  }
}

async function create() {
  try {
    await createCluster(newCluster.value)
    showCreateModal.value = false
    newCluster.value = { name: '', provider: 'mock', num_nodes: 1 }
    await loadClusters()
  } catch (e) {
    alert('Failed to create cluster: ' + e.message)
  }
}

async function launch(id) {
  try {
    await launchCluster(id)
    await loadClusters()
  } catch (e) {
    alert('Failed to launch cluster: ' + e.message)
  }
}

async function stop(id) {
  try {
    await stopCluster(id)
    await loadClusters()
  } catch (e) {
    alert('Failed to stop cluster: ' + e.message)
  }
}

async function terminate(id) {
  if (confirm('Are you sure you want to terminate this cluster?')) {
    try {
      await terminateCluster(id)
      await loadClusters()
    } catch (e) {
      alert('Failed to terminate cluster: ' + e.message)
    }
  }
}

onMounted(loadClusters)
</script>
