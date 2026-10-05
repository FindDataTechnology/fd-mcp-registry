import React from 'react';
import FormField from './FormField';
import { fieldClass, FIELD_FOCUS } from './formClasses';
import { useI18n } from '../../contexts/I18nContext';

interface TagsFieldProps {
  /** Current tags. */
  value: string[];
  /** Called with the parsed tag array (comma-split, trimmed, empties dropped). */
  onChange: (tags: string[]) => void;
  label?: string;
  hint?: React.ReactNode;
  placeholder?: string;
  accent?: keyof typeof FIELD_FOCUS;
}

/**
 * Comma-separated tags input bound to a string[] — the pattern repeated across
 * the server/agent/virtual-server forms (`value={tags.join(',')}` +
 * split/trim/filter on change). Forms that keep tags as a raw string (e.g. the
 * skill form, which parses on save) use a plain input instead.
 */
const TagsField: React.FC<TagsFieldProps> = ({
  value,
  onChange,
  label,
  hint,
  placeholder = 'tag1,tag2,tag3',
  accent = 'purple',
}) => {
  const { t } = useI18n();
  return (
    <FormField label={label ?? t('Tags')} hint={hint}>
      <input
        type="text"
        value={value.join(',')}
        onChange={(e) =>
          onChange(
            e.target.value
              .split(',')
              .map((tag) => tag.trim())
              .filter((tag) => tag),
          )
        }
        className={fieldClass(accent)}
        placeholder={placeholder}
      />
    </FormField>
  );
};

export default TagsField;
