import { createApp } from 'vue'
import { createPinia } from 'pinia'
import { createRouter, createWebHistory } from 'vue-router'
import App from './App.vue'

// Routes
const routes = [
    { path: '/', name: 'Dashboard', component: () => import('./views/Dashboard.vue') },
    { path: '/clusters', name: 'Clusters', component: () => import('./views/Clusters.vue') },
    { path: '/clusters/:id', name: 'ClusterDetail', component: () => import('./views/ClusterDetail.vue') },
    { path: '/jobs', name: 'Jobs', component: () => import('./views/Jobs.vue') },
    { path: '/jobs/:id', name: 'JobDetail', component: () => import('./views/JobDetail.vue') },
]

const router = createRouter({
    history: createWebHistory(),
    routes
})

const app = createApp(App)
app.use(createPinia())
app.use(router)
app.mount('#app')
