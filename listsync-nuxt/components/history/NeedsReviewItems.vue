<template>
  <div class="space-y-4">
    <!-- Search & Filters -->
    <Card class="border border-amber-500/30 hover:border-amber-400/50 transition-all duration-300">
      <div class="p-3">
        <div class="grid grid-cols-1 md:grid-cols-3 gap-3">
          <Input
            v-model="searchQuery"
            placeholder="Search items awaiting review..."
            :icon="SearchIcon"
            @input="debouncedSearch"
          />
          <Select
            v-model="mediaTypeFilter"
            :options="mediaTypeOptions"
            @update:model-value="() => { currentPage = 1; fetchItems() }"
          />
          <Select
            v-model="viewStatus"
            :options="viewOptions"
            @update:model-value="() => { currentPage = 1; fetchItems() }"
          />
        </div>
      </div>
    </Card>

    <!-- Stats Summary -->
    <Card class="border border-amber-500/30 hover:border-amber-400/50 transition-all duration-300">
      <div class="p-3">
        <div class="flex items-center gap-2.5">
          <div class="p-2 rounded-lg bg-gradient-to-br from-amber-600/20 to-amber-500/10 border border-amber-500/30">
            <SearchCheckIcon class="w-4 h-4 text-amber-400" />
          </div>
          <div>
            <p class="text-[10px] text-muted-foreground font-medium">{{ showingDismissed ? 'Dismissed' : 'Awaiting Review' }}</p>
            <p class="text-2xl font-bold text-foreground leading-none">{{ totalItems }}</p>
          </div>
        </div>
      </div>
    </Card>

    <!-- Explanation -->
    <div class="bg-amber-500/10 border border-amber-500/30 rounded-lg p-3">
      <div class="flex items-start gap-2">
        <InfoIcon class="w-4 h-4 text-amber-400 flex-shrink-0 mt-0.5" />
        <p class="text-xs text-muted-foreground">
          These items could not be matched to an exact TMDB/IMDb ID, so strict mode did
          <span class="text-foreground font-medium">not</span> request them (to avoid requesting the wrong title).
          Use the search links to find the correct entry and request it manually in your Seerr instance.
        </p>
      </div>
    </div>

    <!-- Loading State -->
    <div v-if="loading" class="flex items-center justify-center py-20">
      <LoadingSpinner size="lg" text="Loading review queue..." />
    </div>

    <!-- Items Grid -->
    <template v-else-if="items.length > 0">
      <div class="space-y-4">
        <Card
          v-for="item in items"
          :key="item.id"
          class="hover:border-amber-500/30 transition-all hover:shadow-lg hover:shadow-amber-500/10"
        >
          <div class="flex items-start gap-4">
            <!-- Poster/Icon -->
            <div class="w-16 h-24 bg-muted/20 rounded-lg flex items-center justify-center flex-shrink-0 overflow-hidden relative">
              <component
                :is="item.media_type === 'tv' ? TvIcon : FilmIcon"
                class="w-8 h-8 text-muted-foreground opacity-50"
              />
              <div class="absolute top-1 right-1 bg-amber-500 rounded-full p-1">
                <SearchCheckIcon class="w-3 h-3 text-white" />
              </div>
            </div>

            <!-- Content -->
            <div class="flex-1 min-w-0">
              <h4 class="font-semibold text-foreground mb-2">{{ item.title }}</h4>
              <div class="flex flex-wrap items-center gap-2 mb-3">
                <Badge :variant="item.media_type === 'movie' ? 'primary' : 'info'">
                  {{ item.media_type }}
                </Badge>
                <Badge v-if="item.year" variant="default">{{ item.year }}</Badge>
                <Badge v-if="showingDismissed" variant="default">
                  <EyeOffIcon class="w-3 h-3 mr-1" />
                  Dismissed
                </Badge>
                <Badge v-else variant="warning">
                  <SearchCheckIcon class="w-3 h-3 mr-1" />
                  Needs Review
                </Badge>

                <NuxtLink
                  v-if="listSourceLabel(item)"
                  to="/lists"
                  class="inline-flex items-center gap-1 px-2 py-1 text-xs font-medium rounded-md bg-purple-500/10 text-purple-400 hover:bg-purple-500/20 transition-colors group"
                >
                  <ListIcon class="w-3 h-3 group-hover:scale-110 transition-transform" />
                  {{ listSourceLabel(item) }}
                </NuxtLink>

                <span v-if="item.last_synced" class="text-xs text-muted-foreground flex items-center gap-1">
                  <ClockIcon class="w-3 h-3" />
                  {{ formatRelativeTime(item.last_synced) }}
                </span>
              </div>

              <!-- Manual-match actions -->
              <div class="flex flex-wrap gap-2">
                <a
                  :href="tmdbSearchUrl(item)"
                  target="_blank"
                  rel="noopener noreferrer"
                  class="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded-md bg-sky-500/10 text-sky-400 hover:bg-sky-500/20 transition-colors"
                >
                  <ExternalLinkIcon class="w-3 h-3" />
                  Search TMDB
                </a>
                <a
                  :href="letterboxdSearchUrl(item)"
                  target="_blank"
                  rel="noopener noreferrer"
                  class="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded-md bg-green-500/10 text-green-400 hover:bg-green-500/20 transition-colors"
                >
                  <ExternalLinkIcon class="w-3 h-3" />
                  Search Letterboxd
                </a>
                <a
                  v-if="overseerrSearchUrl(item)"
                  :href="overseerrSearchUrl(item)"
                  target="_blank"
                  rel="noopener noreferrer"
                  class="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded-md bg-purple-500/10 text-purple-400 hover:bg-purple-500/20 transition-colors"
                >
                  <ExternalLinkIcon class="w-3 h-3" />
                  Request in Seerr
                </a>
                <button
                  type="button"
                  :disabled="busyId === item.id"
                  class="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded-md bg-muted/30 text-muted-foreground hover:bg-muted/50 hover:text-foreground transition-colors disabled:opacity-50"
                  :title="showingDismissed ? 'Put back in the review queue; the next sync processes it again' : 'Acknowledge: syncs skip this item from now on'"
                  @click="toggleDismissed(item)"
                >
                  <component :is="showingDismissed ? Undo2Icon : EyeOffIcon" class="w-3 h-3" />
                  {{ showingDismissed ? 'Restore' : 'Dismiss' }}
                </button>
              </div>
            </div>
          </div>
        </Card>
      </div>

      <!-- Pagination -->
      <Pagination
        :current-page="currentPage"
        :total-items="totalItems"
        :per-page="perPage"
        @update:current-page="currentPage = $event; fetchItems()"
        @update:per-page="perPage = $event; fetchItems()"
      />
    </template>

    <!-- Empty State -->
    <EmptyState
      v-else
      :icon="CheckCircleIcon"
      :title="showingDismissed ? 'Nothing dismissed' : 'Nothing to review'"
      :description="showingDismissed ? 'Dismissed items show up here and can be restored.' : 'Every synced item matched an exact ID — no manual review needed.'"
    />
  </div>
</template>

<script setup lang="ts">
import {
  Search as SearchIcon,
  SearchCheck as SearchCheckIcon,
  Clock as ClockIcon,
  Film as FilmIcon,
  Tv as TvIcon,
  List as ListIcon,
  Info as InfoIcon,
  ExternalLink as ExternalLinkIcon,
  CheckCircle as CheckCircleIcon,
  EyeOff as EyeOffIcon,
  Undo2 as Undo2Icon,
} from 'lucide-vue-next'
import { formatDistanceToNow } from 'date-fns'
import { useDebounceFn } from '@vueuse/core'

interface ReviewItem {
  id: number | string
  title: string
  media_type: string
  year?: number
  last_synced?: string
  list_sources?: Array<{ list_type: string; list_id: string; display_name?: string | null }>
}

// State
const loading = ref(true)
const items = ref<ReviewItem[]>([])
const currentPage = ref(1)
const perPage = ref(25)
const totalItems = ref(0)
const searchQuery = ref('')
const mediaTypeFilter = ref('all')
const overseerrBaseUrl = ref('')
const viewStatus = ref<'needs_review' | 'dismissed'>('needs_review')
const showingDismissed = computed(() => viewStatus.value === 'dismissed')
const busyId = ref<number | string | null>(null)

const viewOptions = [
  { label: 'Awaiting review', value: 'needs_review' },
  { label: 'Dismissed', value: 'dismissed' },
]

const mediaTypeOptions = [
  { label: 'All Types', value: 'all' },
  { label: 'Movies', value: 'movie' },
  { label: 'TV Shows', value: 'tv' },
]

const fetchItems = async () => {
  loading.value = true
  try {
    const api = useApiService()
    const response: any = await api.getNeedsReviewItems(
      currentPage.value,
      perPage.value,
      searchQuery.value,
      mediaTypeFilter.value,
      viewStatus.value
    )
    items.value = response.items || []
    totalItems.value = response.total || 0
  } catch (error) {
    console.error('Error fetching needs-review items:', error)
    items.value = []
    totalItems.value = 0
  } finally {
    loading.value = false
  }
}

const toggleDismissed = async (item: ReviewItem) => {
  busyId.value = item.id
  try {
    await useApiService().setReviewItemDismissed(item.id, !showingDismissed.value)
    await fetchItems()
  } catch (error) {
    console.error('Error updating review item:', error)
  } finally {
    busyId.value = null
  }
}

const debouncedSearch = useDebounceFn(() => {
  currentPage.value = 1
  fetchItems()
}, 500)

// Try to discover the Seerr/Overseerr base URL so we can deep-link to its search
const loadOverseerrBase = async () => {
  try {
    const api = useApiService()
    const cfg: any = await api.getOverseerrConfig()
    overseerrBaseUrl.value = (cfg?.base_url || '').replace(/\/$/, '')
  } catch {
    overseerrBaseUrl.value = ''
  }
}

const queryFor = (item: ReviewItem) =>
  encodeURIComponent(`${item.title}${item.year ? ' ' + item.year : ''}`)

const tmdbSearchUrl = (item: ReviewItem) =>
  `https://www.themoviedb.org/search?query=${queryFor(item)}`

const letterboxdSearchUrl = (item: ReviewItem) =>
  `https://letterboxd.com/search/${encodeURIComponent(item.title)}/`

const overseerrSearchUrl = (item: ReviewItem) =>
  overseerrBaseUrl.value ? `${overseerrBaseUrl.value}/search?query=${queryFor(item)}` : ''

const listSourceLabel = (item: ReviewItem) => {
  const src = item.list_sources?.[0]
  if (!src) return ''
  const prefix = (src.list_type || '').toUpperCase()
  const name = (src.display_name || src.list_id || '').replace(/https?:\/\/[^/]+\//, '')
  return prefix ? `${prefix}: ${name}` : name
}

const formatRelativeTime = (timestamp: string) => {
  try {
    return formatDistanceToNow(new Date(timestamp), { addSuffix: true })
  } catch {
    return 'Unknown'
  }
}

onMounted(() => {
  loadOverseerrBase()
  fetchItems()
})
</script>
