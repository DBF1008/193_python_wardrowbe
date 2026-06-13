import { describe, it, expect } from 'vitest'
import { cn } from '@/lib/utils'

describe('cn utility', () => {
  it('should merge class names', () => {
    const result = cn('text-red-500', 'bg-blue-500')
    expect(result).toBe('text-red-500 bg-blue-500')
  })

  it('should handle conditional classes', () => {
    const isActive = true
    const result = cn('base-class', isActive && 'active-class')
    expect(result).toBe('base-class active-class')
  })

  it('should handle undefined values', () => {
    const result = cn('base-class', undefined, 'another-class')
    expect(result).toBe('base-class another-class')
  })

  it('should merge conflicting Tailwind classes', () => {
    // tailwind-merge should handle conflicts
    const result = cn('p-4', 'p-8')
    expect(result).toBe('p-8')
  })

  it('should handle arrays of classes', () => {
    const result = cn(['class-1', 'class-2'], 'class-3')
    expect(result).toBe('class-1 class-2 class-3')
  })

  it('should handle empty inputs', () => {
    const result = cn()
    expect(result).toBe('')
  })

  it('should handle object notation', () => {
    const result = cn({
      'active': true,
      'disabled': false,
      'visible': true,
    })
    expect(result).toBe('active visible')
  })
})
