<script setup lang="ts">
import type { ChatMessage } from '@/composables/useAsk'
import MessageBubble from './MessageBubble.vue'

defineProps<{ messages: ChatMessage[] }>()
</script>

<template>
  <div class="mx-auto w-full max-w-3xl xl:max-w-4xl space-y-6 px-4 py-6">
    <MessageBubble v-for="m in messages" :key="m.id" :message="m" />
  </div>
</template>
