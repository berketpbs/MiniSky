<template>
  <div>
    <div class="flex justify-between items-center mb-8">
      <h1 class="text-3xl font-bold text-white">Jobs</h1>
      <button 
        @click="showSubmitModal = true"
        class="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition"
      >
        + Submit Job
      </button>
    </div>
    
    <!-- Jobs Table -->
    <div class="bg-gray-800 rounded-lg border border-gray-700 overflow-hidden">
      <table class="w-full">
        <thead class="bg-gray-700">
          <tr>
            <th class="px-6 py-3 text-left text-xs font-medium text-gray-300 uppercase">Name</th>
            <th class="px-6 py-3 text-left text-xs font-medium text-gray-300 uppercase">State</th>
            <th class="px-6 py-3 text-left text-xs font-medium text-gray-300 uppercase">Cluster</th>
            <th class="px-6 py-3 text-left text-xs font-medium text-gray-300 uppercase">Submitted</th>
            <th class="px-6 py-3 text-left text-xs font-medium text-gray-300 uppercase">Exit Code</th>
            <th class="px-6 py-3 text-left text-xs font-medium text-gray-300 uppercase">Actions</th>
          </tr>
        </thead>
        <tbody class="divide-y divide-gray-700">
          <tr v-if="jobs.length === 0">
            <td colspan="6" class="px-6 py-8 text-center text-gray-400">
              No jobs. Submit one to get started.
            </td>
          </tr>
          <tr v-for="job in jobs" :key="job.job_id" class="hover:bg-gray-750">
            <td class="px-6 py-4">
              <router-link :to="`/jobs/${job.job_id}`" class="text-blue-400 hover:underline">
                {{ job.name }}
              </router-link>
            </td>
            <td class="px-6 py-4">
              <span :class="stateClass(job.state)" class="px-2 py-1 rounded text-xs font-medium">
                {{ job.state }}
              </span>
            </td>
            <td class="px-6 py-4 text-gray-300">{{ job.cluster_id || '-' }}</td>
            <td class="px-6 py-4 text-gray-300">{{ formatDate(job.submitted_at) }}</td>
            <td class="px-6 py-4 text-gray-300">{{ job.exit_code ?? '-' }}</td>
            <td class="px-6 py-4">
              <button 
                v-if="['pending', 'running', 'setting_up'].includes(job.state)"
                @click="cancel(job.job_id)"
                class="px-3 py-1 bg-red-600 text-white rounded text-sm hover:bg-red-700"
              >
                Cancel
              </button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    
    <!-- Submit Modal -->
    <div v-if="showSubmitModal" class="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
      <div class="bg-gray-800 rounded-lg p-6 w-full max-w-md border border-gray-700">
        <h2 class="text-xl font-bold text-white mb-4">Submit Job</h2>
        <form @submit.prevent="submit">
          <div class="mb-4">
            <label class="block text-sm font-medium text-gray-300 mb-2">Name</label>
            <input 
              v-model="newJob.name"
              type="text"
              class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded text-white"
              required
            />
          </div>
          <div class="mb-4">
            <label class="block text-sm font-medium text-gray-300 mb-2">Entrypoint</label>
            <input 
              v-model="newJob.entrypoint"
              type="text"
              placeholder="python train.py"
              class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded text-white"
              required
            />
          </div>
          <div class="mb-4">
            <label class="block text-sm font-medium text-gray-300 mb-2">Cluster (optional)</label>
            <select 
              v-model="newJob.cluster_id"
              class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded text-white"
            >
              <option value="">Auto-select</option>
              <option v-for="c in clusters" :key="c.cluster_id" :value="c.cluster_id">
                {{ c.name }} ({{ c.state }})
              </option>
            </select>
          </div>
          <div class="mb-4">
            <label class="flex items-center text-gray-300">
              <input 
                v-model="newJob.spot_recovery"
                type="checkbox"
                class="mr-2"
              />
              Enable spot recovery
            </label>
          </div>
          <div class="flex justify-end space-x-3">
            <button 
              type="button"
              @click="showSubmitModal = false"
              class="px-4 py-2 bg-gray-600 text-white rounded hover:bg-gray-700"
            >
              Cancel
            </button>
            <button 
              type="submit"
              class="px-4 py-2 bg-blue-600 text-white rounded hover:bg-blue-700"
            >
              Submit
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

const { listJobs, listClusters, submitJob, cancelJob } = useApi()

const jobs = ref([])
const clusters = ref([])
const showSubmitModal = ref(false)
const newJob = ref({
  name: '',
  entrypoint: '',
  task_yaml: '',
  cluster_id: '',
  spot_recovery: false,
  max_restarts: 0
})

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

async function loadData() {
  try {
    jobs.value = await listJobs()
    clusters.value = await listClusters()
  } catch (e) {
    console.error('Failed to load data:', e)
  }
}

async function submit() {
  try {
    const data = { ...newJob.value }
    if (!data.cluster_id) delete data.cluster_id
    await submitJob(data)
    showSubmitModal.value = false
    newJob.value = { name: '', entrypoint: '', task_yaml: '', cluster_id: '', spot_recovery: false, max_restarts: 0 }
    await loadData()
  } catch (e) {
    alert('Failed to submit job: ' + e.message)
  }
}

async function cancel(id) {
  if (confirm('Are you sure you want to cancel this job?')) {
    try {
      await cancelJob(id)
      await loadData()
    } catch (e) {
      alert('Failed to cancel job: ' + e.message)
    }
  }
}

onMounted(loadData)
</script>
