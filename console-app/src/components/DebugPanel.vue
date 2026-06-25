<script setup lang="ts">
import { ref } from 'vue'
import { storeToRefs } from 'pinia'
import { useSession } from '@/stores/session'
import { diagLines } from '@/lib/diag'

// ?debug=1 时的诊断抽屉：集中显示会话态 + 免登/接口失败打点。仅排错用，不影响正常流程。
const { ready, error, role, canManage, token } = storeToRefs(useSession())
const lines = diagLines()
const open = ref(true)
</script>

<template>
  <div class="fixed bottom-2 right-2 z-50 w-80 max-w-[92vw] overflow-hidden rounded-lg border border-border bg-card/95 font-mono text-[11px] shadow-xl backdrop-blur">
    <button type="button" class="flex w-full items-center justify-between bg-secondary px-3 py-1.5 font-bold text-foreground" @click="open = !open">
      <span>诊断面板 · debug</span><span>{{ open ? '▾' : '▸' }}</span>
    </button>
    <div v-show="open" class="max-h-[40vh] overflow-auto p-3">
      <div class="text-muted-foreground">
        ready={{ ready }} · role={{ role }} · canManage={{ canManage }} · token={{ token ? 'set' : '-' }}
      </div>
      <div v-if="error" class="mt-0.5 text-destructive">error: {{ error }}</div>
      <div class="mt-2 space-y-0.5 border-t border-border pt-2">
        <div v-for="l in lines" :key="l.seq" class="break-words text-foreground/80">{{ l.seq }}. {{ l.msg }}</div>
        <div v-if="!lines.length" class="text-muted-foreground">（暂无打点）</div>
      </div>
    </div>
  </div>
</template>
