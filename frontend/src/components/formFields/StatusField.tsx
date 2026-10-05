import React from 'react';
import FormField from './FormField';
import { fieldClass, FIELD_FOCUS } from './formClasses';
import { useI18n } from '../../contexts/I18nContext';

export type LifecycleStatus = 'active' | 'draft' | 'deprecated' | 'beta';

interface StatusFieldProps {
  value: LifecycleStatus;
  onChange: (status: LifecycleStatus) => void;
  label?: string;
  accent?: keyof typeof FIELD_FOCUS;
}

/**
 * The lifecycle-status select (Active/Draft/Beta/Deprecated) shared by the
 * server, agent, and skill forms.
 */
const StatusField: React.FC<StatusFieldProps> = ({
  value,
  onChange,
  label,
  accent = 'purple',
}) => {
  const { t } = useI18n();
  return (
    <FormField label={label ?? t('Lifecycle Status')}>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value as LifecycleStatus)}
        className={fieldClass(accent)}
      >
        <option value="active">{t('Active')}</option>
        <option value="draft">{t('Draft')}</option>
        <option value="beta">{t('Beta')}</option>
        <option value="deprecated">{t('Deprecated')}</option>
      </select>
    </FormField>
  );
};

export default StatusField;
