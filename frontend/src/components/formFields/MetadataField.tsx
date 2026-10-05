import React from 'react';
import FormField from './FormField';
import { FIELD_BASE, FIELD_FOCUS } from './formClasses';
import { useI18n } from '../../contexts/I18nContext';

interface MetadataFieldProps {
  value: string;
  onChange: (value: string) => void;
  label?: string;
  hint?: React.ReactNode;
  placeholder?: string;
  rows?: number;
  accent?: keyof typeof FIELD_FOCUS;
}

const DEFAULT_PLACEHOLDER =
  '{"team": "platform", "owner": "alice@example.com"}';

/**
 * The "Custom Metadata (JSON, optional)" textarea shared by the server, agent,
 * and skill forms — a monospace textarea with a hint line.
 */
const MetadataField: React.FC<MetadataFieldProps> = ({
  value,
  onChange,
  label,
  hint,
  placeholder = DEFAULT_PLACEHOLDER,
  rows = 4,
  accent = 'purple',
}) => {
  const { t } = useI18n();
  return (
    <FormField
      label={label ?? t('Custom Metadata (JSON, optional)')}
      hint={hint ?? t('Custom key-value pairs in JSON format for searchable metadata')}
    >
      <textarea
        value={value}
        onChange={(e) => onChange(e.target.value)}
        rows={rows}
        className={`${FIELD_BASE} ${FIELD_FOCUS[accent]} font-mono text-sm`}
        placeholder={placeholder}
      />
    </FormField>
  );
};

export default MetadataField;
