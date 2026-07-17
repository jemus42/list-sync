<template>
  <div class="space-y-6">
    <!-- Page Header -->
    <div class="flex flex-col md:flex-row md:items-center justify-between gap-4">
      <div>
        <h1 class="text-4xl font-bold text-foreground titillium-web-bold bg-gradient-to-r from-amber-400 to-amber-600 bg-clip-text text-transparent">
          Review Queue
        </h1>
        <p class="text-muted-foreground mt-2 text-base">
          Items with no exact ID match — not requested, awaiting manual review
        </p>
      </div>

      <div class="flex gap-3">
        <Button
          variant="ghost"
          :icon="ArrowLeftIcon"
          @click="$router.back()"
        >
          Back
        </Button>

        <Button
          variant="primary"
          :icon="RefreshIcon"
          :loading="isRefreshing"
          @click="refreshData"
        >
          Refresh
        </Button>
      </div>
    </div>

    <!-- Needs Review Component -->
    <NeedsReviewItems />
  </div>
</template>

<script setup lang="ts">
import {
  RefreshCw as RefreshIcon,
  ArrowLeft as ArrowLeftIcon,
} from 'lucide-vue-next'

useHead({
  title: 'Review Queue',
})

const isRefreshing = ref(false)

const refreshData = async () => {
  isRefreshing.value = true
  setTimeout(() => {
    window.location.reload()
  }, 100)
}
</script>
