import { ref, onUnmounted } from 'vue'

export function useWebSocket(url) {
  const ws = ref(null)
  const isConnected = ref(false)
  const lastMessage = ref(null)
  const error = ref(null)

  let reconnectTimer = null
  let reconnectAttempts = 0
  const maxReconnectAttempts = 5

  const connect = () => {
    try {
      ws.value = new WebSocket(url)

      ws.value.onopen = () => {
        isConnected.value = true
        error.value = null
        reconnectAttempts = 0
        console.log(`WebSocket connected to ${url}`)
      }

      ws.value.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data)
          lastMessage.value = data
        } catch (e) {
          console.error('Failed to parse WebSocket message', e)
        }
      }

      ws.value.onclose = () => {
        isConnected.value = false
        console.log(`WebSocket disconnected from ${url}`)
        
        // Auto-reconnect
        if (reconnectAttempts < maxReconnectAttempts) {
          reconnectAttempts++
          const timeout = Math.min(1000 * Math.pow(2, reconnectAttempts), 10000)
          console.log(`Attempting to reconnect in ${timeout}ms...`)
          reconnectTimer = setTimeout(connect, timeout)
        } else {
          error.value = 'Max reconnect attempts reached'
        }
      }

      ws.value.onerror = (e) => {
        console.error('WebSocket error:', e)
        error.value = 'Connection error'
      }
    } catch (e) {
      error.value = e.message
      console.error('WebSocket connection failed:', e)
    }
  }

  const disconnect = () => {
    if (reconnectTimer) {
      clearTimeout(reconnectTimer)
    }
    if (ws.value) {
      ws.value.close()
    }
  }

  const sendMessage = (message) => {
    if (ws.value && isConnected.value) {
      ws.value.send(JSON.stringify(message))
    } else {
      console.warn('WebSocket is not connected. Cannot send message.')
    }
  }

  // Connect on setup
  connect()

  // Clean up on component unmount
  onUnmounted(() => {
    disconnect()
  })

  return {
    ws,
    isConnected,
    lastMessage,
    error,
    sendMessage,
    disconnect,
    connect
  }
}
