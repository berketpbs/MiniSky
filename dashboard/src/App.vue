<template>
  <div class="min-h-screen bg-gray-900">
    <!-- Navigation -->
    <nav class="bg-gray-800 border-b border-gray-700">
      <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
        <div class="flex items-center justify-between h-16">
          <div class="flex items-center">
            <div class="flex-shrink-0">
              <span class="text-2xl font-bold text-blue-400">☁️ MiniSky</span>
            </div>
            <div class="hidden md:block ml-10">
              <div class="flex items-baseline space-x-4">
                <router-link 
                  to="/" 
                  class="px-3 py-2 rounded-md text-sm font-medium"
                  :class="$route.path === '/' ? 'bg-gray-900 text-white' : 'text-gray-300 hover:bg-gray-700'"
                >
                  Dashboard
                </router-link>
                <router-link 
                  to="/clusters" 
                  class="px-3 py-2 rounded-md text-sm font-medium"
                  :class="$route.path.startsWith('/clusters') ? 'bg-gray-900 text-white' : 'text-gray-300 hover:bg-gray-700'"
                >
                  Clusters
                </router-link>
                <router-link 
                  to="/jobs" 
                  class="px-3 py-2 rounded-md text-sm font-medium"
                  :class="$route.path.startsWith('/jobs') ? 'bg-gray-900 text-white' : 'text-gray-300 hover:bg-gray-700'"
                >
                  Jobs
                </router-link>
              </div>
            </div>
          </div>
          <div class="flex items-center">
            <span class="text-sm text-gray-400">
              API: <span :class="apiStatus === 'healthy' ? 'text-green-400' : 'text-red-400'">
                {{ apiStatus }}
              </span>
            </span>
          </div>
        </div>
      </div>
    </nav>

    <!-- Main Content -->
    <main class="max-w-7xl mx-auto py-6 px-4 sm:px-6 lg:px-8">
      <router-view />
    </main>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { useApi } from './composables/useApi'

const { checkHealth } = useApi()
const apiStatus = ref('checking...')

onMounted(async () => {
  try {
    await checkHealth()
    apiStatus.value = 'healthy'
  } catch (e) {
    apiStatus.value = 'offline'
  }
})
</script>
