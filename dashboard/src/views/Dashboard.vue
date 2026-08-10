<template>
  <div>
    <h1 class="text-3xl font-bold text-white mb-8">Dashboard</h1>
    
    <!-- Stats Cards -->
    <div class="grid grid-cols-1 md:grid-cols-3 gap-6 mb-8">
      <div class="bg-gray-800 rounded-lg p-6 border border-gray-700">
        <div class="flex items-center">
          <div class="p-3 rounded-full bg-blue-500 bg-opacity-20">
            <span class="text-2xl">🖥️</span>
          </div>
          <div class="ml-4">
            <p class="text-sm text-gray-400">Active Clusters</p>
            <p class="text-2xl font-bold text-white">{{ stats.activeClusters }}</p>
          </div>
        </div>
      </div>
      
      <div class="bg-gray-800 rounded-lg p-6 border border-gray-700">
        <div class="flex items-center">
          <div class="p-3 rounded-full bg-green-500 bg-opacity-20">
            <span class="text-2xl">⚡</span>
          </div>
          <div class="ml-4">
            <p class="text-sm text-gray-400">Running Jobs</p>
            <p class="text-2xl font-bold text-white">{{ stats.runningJobs }}</p>
          </div>
        </div>
      </div>
      
      <div class="bg-gray-800 rounded-lg p-6 border border-gray-700">
        <div class="flex items-center">
          <div class="p-3 rounded-full bg-purple-500 bg-opacity-20">
            <span class="text-2xl">🎮</span>
          </div>
          <div class="ml-4">
            <p class="text-sm text-gray-400">Total GPUs</p>
            <p class="text-2xl font-bold text-white">{{ stats.totalGpus }}</p>
          </div>
        </div>
      </div>
    </div>
    
    <!-- Recent Activity -->
    <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
      <!-- Recent Clusters -->
      <div class="bg-gray-800 rounded-lg border border-gray-700">
        <div class="px-6 py-4 border-b border-gray-700">
          <h2 class="text-lg font-semibold text-white">Recent Clusters</h2>
        </div>
        <div class="p-6">
          <div v-if="clusters.length === 0" class="text-gray-400 text-center py-4">
            No clusters yet
          </div>
          <div v-else class="space-y-4">
            <div 
              v-for="cluster in clusters.slice(0, 5)" 
              :key="cluster.cluster_id"
              class="flex items-center justify-between p-3 bg-gray-700 rounded-lg"
            >
              <div>
                <p class="text-white font-medium">{{ cluster.name }}</p>
                <p class="text-sm text-gray-400">{{ cluster.provider }}</p>
              </div>
              <span :class="stateClass(cluster.state)" class="px-2 py-1 rounded text-xs font-medium">
                {{ cluster.state }}
              </span>
            </div>
          </div>
        </div>
      </div>
      
      <!-- Recent Jobs -->
      <div class="bg-gray-800 rounded-lg border border-gray-700">
        <div class="px-6 py-4 border-b border-gray-700">
          <h2 class="text-lg font-semibold text-white">Recent Jobs</h2>
        </div>
        <div class="p-6">
          <div v-if="jobs.length === 0" class="text-gray-400 text-center py-4">
            No jobs yet
          </div>
          <div v-else class="space-y-4">
            <div 
              v-for="job in jobs.slice(0, 5)" 
              :key="job.job_id"
              class="flex items-center justify-between p-3 bg-gray-700 rounded-lg"
            >
              <div>
                <p class="text-white font-medium">{{ job.name }}</p>
                <p class="text-sm text-gray-400">{{ job.cluster_id || 'No cluster' }}</p>
              </div>
              <span :class="jobStateClass(job.state)" class="px-2 py-1 rounded text-xs font-medium">
                {{ job.state }}
              </span>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, watch } from 'vue'
import { useApi } from '../composables/useApi'
import { useWebSocket } from '../composables/useWebSocket'

const { listClusters, listJobs } = useApi()
const wsUrl = `ws://${window.location.hostname}:8000/v1/ws`
const { lastMessage, isConnected } = useWebSocket(wsUrl)

const clusters = ref([])
const jobs = ref([])

const stats = computed(() => ({
  activeClusters: clusters.value.filter(c => c.state === 'up').length,
  runningJobs: jobs.value.filter(j => ['pending', 'running', 'setting_up'].includes(j.state)).length,
  totalGpus: clusters.value.reduce((sum, c) => {
    if (c.accelerators) {
      return sum + Object.values(c.accelerators).reduce((a, b) => a + b, 0)
    }
    return sum
  }, 0)
}))

function stateClass(state) {
  const classes = {
    up: 'bg-green-500 text-white',
    launching: 'bg-yellow-500 text-black',
    stopped: 'bg-gray-500 text-white',
    terminated: 'bg-red-500 text-white',
    error: 'bg-red-600 text-white'
  }
  return classes[state] || 'bg-gray-500 text-white'
}

function jobStateClass(state) {
  const classes = {
    succeeded: 'bg-green-500 text-white',
    running: 'bg-blue-500 text-white',
    pending: 'bg-yellow-500 text-black',
    failed: 'bg-red-500 text-white',
    cancelled: 'bg-gray-500 text-white'
  }
  return classes[state] || 'bg-gray-500 text-white'
}

// Watch for WebSocket events
watch(lastMessage, (event) => {
  if (!event) return
  
  if (event.type === 'cluster_state_change') {
    const idx = clusters.value.findIndex(c => c.cluster_id === event.payload.cluster_id)
    if (idx !== -1) {
      clusters.value[idx].state = event.payload.new_state
    } else {
      // Fetch full list again if it's a new cluster
      loadData()
    }
  } else if (event.type === 'job_state_change') {
    const idx = jobs.value.findIndex(j => j.job_id === event.payload.job_id)
    if (idx !== -1) {
      jobs.value[idx].state = event.payload.new_state
    } else {
      loadData()
    }
  }
})

async function loadData() {
  try {
    clusters.value = await listClusters()
    jobs.value = await listJobs()
  } catch (e) {
    console.error('Failed to load data:', e)
  }
}

onMounted(() => {
  loadData()
})
</script>
