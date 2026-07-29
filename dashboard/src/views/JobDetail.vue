<template>
  <div>
    <div class="mb-6">
      <router-link to="/jobs" class="text-blue-400 hover:underline">← Back to Jobs</router-link>
    </div>
    
    <div v-if="job" class="bg-gray-800 rounded-lg border border-gray-700 p-6">
      <div class="flex justify-between items-start mb-6">
        <div>
          <h1 class="text-3xl font-bold text-white">{{ job.name }}</h1>
          <p class="text-gray-400">{{ job.job_id }}</p>
        </div>
        <span :class="stateClass(job.state)" class="px-3 py-1 rounded text-sm font-medium">
          {{ job.state }}
        </span>
      </div>
      
      <div class="grid grid-cols-2 md:grid-cols-4 gap-6 mb-6">
        <div>
          <p class="text-sm text-gray-400">Cluster</p>
          <p class="text-white font-medium">{{ job.cluster_id || '-' }}</p>
        </div>
        <div>
          <p class="text-sm text-gray-400">Submitted</p>
          <p class="text-white font-medium">{{ formatDate(job.submitted_at) }}</p>
        </div>
        <div>
          <p class="text-sm text-gray-400">Started</p>
          <p class="text-white font-medium">{{ formatDate(job.started_at) }}</p>
        </div>
        <div>
          <p class="text-sm text-gray-400">Ended</p>
          <p class="text-white font-medium">{{ formatDate(job.ended_at) }}</p>
        </div>
      </div>
      
      <div v-if="job.exit_code !== null" class="mb-6">
        <p class="text-sm text-gray-400">Exit Code</p>
        <p :class="job.exit_code === 0 ? 'text-green-400' : 'text-red-400'" class="font-medium">
          {{ job.exit_code }}
        </p>
      </div>
      
      <div v-if="job.failure_reason" class="mb-6 p-4 bg-red-900 bg-opacity-30 rounded border border-red-700">
        <p class="text-sm text-red-400 font-medium">Failure Reason</p>
        <p class="text-red-300">{{ job.failure_reason }}</p>
      </div>
      
      <div class="flex space-x-3">
        <button 
          v-if="['pending', 'running', 'setting_up'].includes(job.state)"
          @click="cancel"
          class="px-4 py-2 bg-red-600 text-white rounded hover:bg-red-700"
        >
          Cancel Job
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
const { getJob, cancelJob } = useApi()

const job = ref(null)

function stateClass(state) {
  const classes = {
    succeeded: 'bg-green-500 text-white',
    running: 'bg-blue-500 text-white',
    pending: 'bg-yellow-500 text-black',
    setting_up: 'bg-yellow-500 text-black',
    failed: 'bg-red-500 text-white',
    cancelled: 'bg-gray-500 text-white'
  }
  return classes[state] || 'bg-gray-500 text-white'
}

function formatDate(dateStr) {
  if (!dateStr) return '-'
  return new Date(dateStr).toLocaleString()
}

async function loadJob() {
  try {
    job.value = await getJob(route.params.id)
  } catch (e) {
    console.error('Failed to load job:', e)
  }
}

async function cancel() {
  if (confirm('Are you sure you want to cancel this job?')) {
    try {
      await cancelJob(route.params.id)
      await loadJob()
    } catch (e) {
      alert('Failed: ' + e.message)
    }
  }
}

onMounted(loadJob)
</script>
