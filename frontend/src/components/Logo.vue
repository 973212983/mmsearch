<script setup>
defineProps({ size: { type: Number, default: 32 } })
</script>

<template>
  <svg :width="size" :height="size" viewBox="0 0 64 64" class="logo">
    <defs>
      <linearGradient id="lg" x1="0" y1="0" x2="1" y2="1">
        <stop offset="0" stop-color="#6366f1" />
        <stop offset="1" stop-color="#a855f7" />
      </linearGradient>
    </defs>
    <rect x="2" y="2" width="60" height="60" rx="16" fill="url(#lg)" />
    <circle cx="28" cy="28" r="13" fill="rgba(255,255,255,.95)" stroke="#fff" stroke-width="4" />
    <path d="M38 38l11 11" stroke="#fff" stroke-width="6" stroke-linecap="round" />
    <rect x="21" y="21" width="6" height="6" rx="1.6" fill="#3b82f6" />
    <rect x="29" y="21" width="6" height="6" rx="1.6" fill="#10b981" />
    <rect x="21" y="29" width="6" height="6" rx="1.6" fill="#f59e0b" />
    <rect x="29" y="29" width="6" height="6" rx="1.6" fill="#8b5cf6" />
  </svg>
</template>

<style scoped>
.logo { filter: drop-shadow(0 10px 20px rgba(99, 102, 241, .35)); }
</style>
