<template>
  <div>
    <div class="mb-6">
      <router-link to="/clusters" class="text-blue-400 hover:underline">← Back to Clusters</router-link>
    </div>
    
    <div v-if="cluster" class="bg-gray-800 rounded-lg border border-gray-700 p-6">
      <div class="flex justify-between items-start mb-6">
        <div>
          <h1 class="text-3xl font-bold text-white">{{ cluster.name }}</h1>
          <p class="text-gray-400">{{ cluster.cluster_id }}</p>
        </div>
        <span :class="stateClass(cluster.state)" class="px-3 py-1 rounded text-sm font-medium">
          {{ cluster.state }}
        </span>
      </div>
      
      <div class="grid grid-cols-2 md:grid-cols-4 gap-6 mb-6">
        <div>
          <p class="text-sm text-gray-400">Provider</p>
          <p class="text-white font-medium">{{ cluster.provider }}</p>
        </div>
        <div>
          <p class="text-sm text-gray-400">Nodes</p>
          <p class="text-white font-medium">{{ cluster.num_nodes }}</p>
        </div>
        <div>
          <p class="text-sm text-gray-400">Head IP</p>
          <p class="text-white font-medium">{{ cluster.head_ip || '-' }}</p>
        </div>
        <div>
          <p class="text-sm text-gray-400">Launched</p>
          <p class="text-white font-medium">{{ formatDate(cluster.launched_at) }}</p>
        </div>
      </div>
      
      <div class="flex space-x-3">
        <button 
          v-if="cluster.state === 'init'"
          @click="launch"
          class="px-4 py-2 bg-green-600 text-white rounded hover:bg-green-700"
        >
          Launch
        </button>
        <button 
          v-if="cluster.state === 'up'"
          @click="stop"
          class="px-4 py-2 bg-yellow-600 text-white rounded hover:bg-yellow-700"
        >
          Stop
        </button>
        <button 
          v-if="cluster.state !== 'terminated'"
          @click="terminate"
          class="px-4 py-2 bg-red-600 text-white rounded hover:bg-red-700"
        >
          Terminate
        </button>
      </div>
    </div>
    
    <div v-else class="text-center text-gray-400 py-12">
      Loading...
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useApi } from '../composables/useApi'

const route = useRoute()
const router = useRouter()
const { getCluster, launchCluster, stopCluster, terminateCluster } = useApi()

const cluster = ref(null)

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

function formatDate(dateStr) {
  if (!dateStr) return '-'
  return new Date(dateStr).toLocaleString()
}

async function loadCluster() {
  try {
    cluster.value = await getCluster(route.params.id)
  } catch (e) {
    console.error('Failed to load cluster:', e)
  }
}

async function launch() {
  try {
    await launchCluster(route.params.id)
    await loadCluster()
  } catch (e) {
    alert('Failed: ' + e.message)
  }
}

async function stop() {
  try {
    await stopCluster(route.params.id)
    await loadCluster()
  } catch (e) {
    alert('Failed: ' + e.message)
  }
}

async function terminate() {
  if (confirm('Are you sure?')) {
    try {
      await terminateCluster(route.params.id)
      router.push('/clusters')
    } catch (e) {
      alert('Failed: ' + e.message)
    }
  }
}

onMounted(loadCluster)
</script>
